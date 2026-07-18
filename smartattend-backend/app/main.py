from fastapi import FastAPI, Depends, HTTPException, Header, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from typing import List, Optional
import asyncio
import secrets

from . import models, schemas
from .database import engine, get_db, run_simple_migrations, SessionLocal
from .geofence import pick_best_reading, check_location
from .face_verify import verify_face, embedding_to_str
from .auth import hash_pin, verify_pin, hash_password, verify_password
from .ws_manager import manager

models.Base.metadata.create_all(bind=engine)
run_simple_migrations()

app = FastAPI(title="SmartAttend API")


@app.on_event("startup")
async def on_startup():
    manager.set_loop(asyncio.get_running_loop())


# TODO: lock this down to your actual frontend origin before going live
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_or_create_leave_balance(db: Session, faculty_id: int) -> models.LeaveBalance:
    balance = db.query(models.LeaveBalance).filter(models.LeaveBalance.faculty_id == faculty_id).first()
    if not balance:
        balance = models.LeaveBalance(faculty_id=faculty_id)
        db.add(balance)
        db.commit()
        db.refresh(balance)
    return balance


ADMIN_SESSION_TTL_HOURS = 24

# Late-arrival tracking — fixed daily start time for everyone (Pakistan local time).
# Server timestamps are stored in UTC, so we convert before comparing.
PKT_OFFSET_HOURS = 5  # Pakistan Standard Time is UTC+5, no daylight saving
EXPECTED_ARRIVAL_HOUR = 8   # 8:00 AM local — actual campus start time
EXPECTED_ARRIVAL_MINUTE = 0
LATE_GRACE_MINUTES = 10  # arriving up to 10 min after 8:00 doesn't count as "late"


def compute_late_minutes(utc_timestamp: datetime) -> int:
    """
    Returns how many minutes past the 8:00 AM (+ grace period) local arrival
    time this check-in was, or 0 if on time / early. Fixed schedule for
    everyone — no per-teacher or per-class schedule support (yet).
    """
    local_time = utc_timestamp + timedelta(hours=PKT_OFFSET_HOURS)
    expected = local_time.replace(
        hour=EXPECTED_ARRIVAL_HOUR, minute=EXPECTED_ARRIVAL_MINUTE, second=0, microsecond=0
    )
    grace_deadline = expected + timedelta(minutes=LATE_GRACE_MINUTES)
    if local_time <= grace_deadline:
        return 0
    return int((local_time - grace_deadline).total_seconds() // 60)


def get_current_admin(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> models.Admin:
    """
    Protects admin-only endpoints. Expects header: Authorization: Bearer <token>
    Token comes from POST /api/admin/login.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    session = db.query(models.AdminSession).filter(models.AdminSession.token == token).first()
    if not session or session.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Invalid or expired admin session")

    admin = db.query(models.Admin).filter(models.Admin.id == session.admin_id).first()
    if not admin:
        raise HTTPException(status_code=401, detail="Admin account not found")
    return admin


def validate_admin_token(token: str, db: Session) -> Optional[models.Admin]:
    """Shared validation logic used by both the HTTP admin-auth dependency and the WebSocket endpoint."""
    session = db.query(models.AdminSession).filter(models.AdminSession.token == token).first()
    if not session or session.expires_at < datetime.utcnow():
        return None
    return db.query(models.Admin).filter(models.Admin.id == session.admin_id).first()


@app.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket, token: str):
    """
    Admin dashboard connects here (after logging in) to receive live
    attendance events instead of needing to refresh the page. Pass the
    admin access token as a query param: wss://.../ws/admin?token=<token>
    """
    db = SessionLocal()
    try:
        admin = validate_admin_token(token, db)
    finally:
        db.close()

    if not admin:
        await websocket.close(code=4401)  # custom code — invalid/expired token
        return

    await manager.connect(websocket)
    try:
        while True:
            # We don't expect the client to send anything meaningful, but we
            # need to keep awaiting to detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.get("/")
def root():
    return {"status": "ok", "service": "smartattend-backend"}


@app.post("/api/faculty/enroll", response_model=schemas.FacultyOut)
def enroll_faculty(payload: schemas.FacultyEnrollRequest, db: Session = Depends(get_db)):
    existing = db.query(models.Faculty).filter(
        (models.Faculty.email == payload.email) | (models.Faculty.teacher_id == payload.teacher_id)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Faculty with this email or teacher ID already enrolled")

    faculty = models.Faculty(
        name=payload.name,
        email=payload.email,
        teacher_id=payload.teacher_id,
        department=payload.department,
        face_embedding=embedding_to_str(payload.face_embedding),
        pin_hash=hash_pin(payload.pin),
        approval_status="pending",
    )
    db.add(faculty)
    db.commit()
    db.refresh(faculty)
    return faculty


@app.get("/api/faculty", response_model=List[schemas.FacultyOut])
def list_faculty(approval_status: str = None, db: Session = Depends(get_db)):
    q = db.query(models.Faculty)
    if approval_status:
        q = q.filter(models.Faculty.approval_status == approval_status)
    return q.all()


@app.post("/api/faculty/{faculty_id}/approve", response_model=schemas.FacultyOut)
def approve_faculty(
    faculty_id: int,
    payload: schemas.ApprovalRequest,
    db: Session = Depends(get_db),
    admin: models.Admin = Depends(get_current_admin),
):
    """Admin-only — requires a valid admin session token."""
    if payload.approval_status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="approval_status must be 'approved' or 'rejected'")

    faculty = db.query(models.Faculty).filter(models.Faculty.id == faculty_id).first()
    if not faculty:
        raise HTTPException(status_code=404, detail="Faculty not found")

    faculty.approval_status = payload.approval_status
    db.commit()
    db.refresh(faculty)
    return faculty


@app.post("/api/auth/login", response_model=schemas.LoginResponse)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    faculty = db.query(models.Faculty).filter(models.Faculty.teacher_id == payload.teacher_id).first()
    if not faculty or not verify_pin(payload.pin, faculty.pin_hash):
        return schemas.LoginResponse(status="invalid_credentials", faculty=None)

    if faculty.approval_status != "approved":
        return schemas.LoginResponse(status=faculty.approval_status, faculty=faculty)

    return schemas.LoginResponse(status="approved", faculty=faculty)


@app.post("/api/auth/re-enroll-face", response_model=schemas.ReEnrollFaceResponse)
def re_enroll_face(payload: schemas.ReEnrollFaceRequest, db: Session = Depends(get_db)):
    """
    Lets an already-approved teacher replace their stored face descriptor —
    e.g. if it was captured incorrectly before, lighting was bad, or they
    just want to refresh it. Requires teacher_id + PIN, same as login, so a
    stranger can't overwrite someone else's biometric data.
    """
    faculty = db.query(models.Faculty).filter(models.Faculty.teacher_id == payload.teacher_id).first()
    if not faculty or not verify_pin(payload.pin, faculty.pin_hash):
        return schemas.ReEnrollFaceResponse(status="invalid_credentials", faculty=None)

    if faculty.approval_status != "approved":
        return schemas.ReEnrollFaceResponse(status="not_approved", faculty=faculty)

    faculty.face_embedding = embedding_to_str(payload.face_embedding)
    db.commit()
    db.refresh(faculty)

    return schemas.ReEnrollFaceResponse(status="ok", faculty=faculty)


MAX_PHOTO_BASE64_CHARS = 500_000  # ~375KB binary — plenty for a resized profile pic, keeps DB rows small


@app.post("/api/faculty/upload-photo", response_model=schemas.UploadPhotoResponse)
def upload_photo(payload: schemas.UploadPhotoRequest, db: Session = Depends(get_db)):
    """
    Lets a teacher upload/replace their own profile picture, synced across
    devices via the backend instead of being stuck in one device's local
    storage. Requires teacher_id + PIN, same pattern as re-enroll-face.
    Frontend should resize/compress the image (e.g. to ~200x200) before
    sending, to keep payloads small.
    """
    faculty = db.query(models.Faculty).filter(models.Faculty.teacher_id == payload.teacher_id).first()
    if not faculty or not verify_pin(payload.pin, faculty.pin_hash):
        return schemas.UploadPhotoResponse(status="invalid_credentials", faculty=None)

    if faculty.approval_status != "approved":
        return schemas.UploadPhotoResponse(status="not_approved", faculty=faculty)

    if len(payload.photo_base64) > MAX_PHOTO_BASE64_CHARS:
        return schemas.UploadPhotoResponse(status="too_large", faculty=faculty)

    faculty.profile_photo = payload.photo_base64
    db.commit()
    db.refresh(faculty)

    return schemas.UploadPhotoResponse(status="ok", faculty=faculty)


@app.post("/api/admin/bootstrap", response_model=schemas.AdminLoginResponse)
def bootstrap_admin(payload: schemas.AdminBootstrapRequest, db: Session = Depends(get_db)):
    """
    Creates the FIRST admin account. Only works if no admin exists yet.
    The dashboard has no signup UI, so run this once yourself via curl/Postman:
      curl -X POST <backend-url>/api/admin/bootstrap -H "Content-Type: application/json" \
        -d '{"email":"you@example.com","password":"yourpassword","name":"Your Name"}'
    Then log in normally through the dashboard.
    """
    existing_count = db.query(models.Admin).count()
    if existing_count > 0:
        raise HTTPException(status_code=400, detail="An admin already exists — use the dashboard login instead")

    admin = models.Admin(
        email=payload.email,
        name=payload.name or payload.email,
        password_hash=hash_password(payload.password),
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=ADMIN_SESSION_TTL_HOURS)
    db.add(models.AdminSession(token=token, admin_id=admin.id, expires_at=expires_at))
    db.commit()

    return schemas.AdminLoginResponse(
        access_token=token,
        admin=schemas.AdminOut(name=admin.name, email=admin.email),
    )


@app.post("/api/admin/login", response_model=schemas.AdminLoginResponse)
def admin_login(payload: schemas.AdminLoginRequest, db: Session = Depends(get_db)):
    admin = db.query(models.Admin).filter(models.Admin.email == payload.email).first()
    if not admin or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=ADMIN_SESSION_TTL_HOURS)
    db.add(models.AdminSession(token=token, admin_id=admin.id, expires_at=expires_at))
    db.commit()

    return schemas.AdminLoginResponse(
        access_token=token,
        admin=schemas.AdminOut(name=admin.name, email=admin.email),
    )


@app.post("/api/attendance/check-in", response_model=schemas.CheckInResponse)
def check_in(payload: schemas.CheckInRequest, db: Session = Depends(get_db)):
    faculty = db.query(models.Faculty).filter(models.Faculty.id == payload.faculty_id).first()
    if not faculty:
        raise HTTPException(status_code=404, detail="Faculty not found")

    if faculty.approval_status != "approved":
        raise HTTPException(status_code=403, detail=f"Faculty is not approved (status: {faculty.approval_status})")

    # 1. Pick best GPS reading from the batch sent by frontend
    best_reading = pick_best_reading(payload.gps_readings)

    # 2. Check geofence
    location_check = check_location(best_reading)

    # 3. Verify face
    face_result = verify_face(payload.face_embedding, faculty.face_embedding)

    # 4. Decide final status
    if not location_check["allowed"]:
        status = "rejected_location"
    elif face_result["verified"] != "pass":
        status = "rejected_face"
    else:
        status = "present"

    record = models.AttendanceRecord(
        faculty_id=faculty.id,
        timestamp=datetime.utcnow(),
        latitude=best_reading.latitude,
        longitude=best_reading.longitude,
        gps_accuracy=best_reading.accuracy,
        readings_used=len(payload.gps_readings),
        wifi_ssid=payload.wifi_ssid,
        face_match_score=face_result["score"],
        face_verified=face_result["verified"],
        status=status,
        notes=location_check["reason"],
        record_type="check_in",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    # Count this as an attended working day (once per calendar day, only if present)
    # and track late-arrival minutes against the fixed 9:00 AM start time.
    if status == "present":
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        already_counted_today = (
            db.query(models.AttendanceRecord)
            .filter(
                models.AttendanceRecord.faculty_id == faculty.id,
                models.AttendanceRecord.record_type == "check_in",
                models.AttendanceRecord.status == "present",
                models.AttendanceRecord.timestamp >= today_start,
                models.AttendanceRecord.id != record.id,
            )
            .first()
        )
        if not already_counted_today:
            balance = get_or_create_leave_balance(db, faculty.id)
            balance.working_days_attended += 1

            late_minutes = compute_late_minutes(record.timestamp)
            if late_minutes > 0:
                balance.late_margin_used_minutes += late_minutes

            db.commit()

    manager.broadcast_threadsafe({
        "event": "attendance",
        "data": {
            "record_id": record.id,
            "faculty_id": faculty.id,
            "faculty_name": faculty.name,
            "department": faculty.department,
            "record_type": "check_in",
            "status": status,
            "timestamp": record.timestamp.isoformat(),
            "face_match_score": face_result["score"],
            "distance_to_boundary_m": location_check["distance_to_boundary_m"],
        },
    })

    return schemas.CheckInResponse(
        status=status,
        reason=location_check["reason"] if status != "present" else face_result.get("reason"),
        distance_to_boundary_m=location_check["distance_to_boundary_m"],
        face_match_score=face_result["score"],
        gps_accuracy_used=best_reading.accuracy,
        record_id=record.id,
    )


@app.get("/api/attendance", response_model=List[schemas.AttendanceOut])
def list_attendance(faculty_id: int = None, db: Session = Depends(get_db)):
    q = db.query(models.AttendanceRecord)
    if faculty_id:
        q = q.filter(models.AttendanceRecord.faculty_id == faculty_id)
    records = q.order_by(models.AttendanceRecord.timestamp.desc()).all()

    faculty_lookup = {f.id: f for f in db.query(models.Faculty).all()}
    return [
        schemas.AttendanceOut(
            id=r.id,
            faculty_id=r.faculty_id,
            faculty_name=faculty_lookup.get(r.faculty_id).name if faculty_lookup.get(r.faculty_id) else None,
            department=faculty_lookup.get(r.faculty_id).department if faculty_lookup.get(r.faculty_id) else None,
            timestamp=r.timestamp,
            latitude=r.latitude,
            longitude=r.longitude,
            gps_accuracy=r.gps_accuracy,
            wifi_ssid=r.wifi_ssid,
            face_match_score=r.face_match_score,
            status=r.status,
            record_type=r.record_type,
        )
        for r in records
    ]


@app.post("/api/attendance/check-out", response_model=schemas.CheckInResponse)
def check_out(payload: schemas.CheckOutRequest, db: Session = Depends(get_db)):
    """
    Marks the teacher's exit from campus. Same GPS + face verification as
    check-in, but does NOT log the teacher out of the app and does NOT
    affect leave/attendance counters — it's just an exit timestamp record.
    """
    faculty = db.query(models.Faculty).filter(models.Faculty.id == payload.faculty_id).first()
    if not faculty:
        raise HTTPException(status_code=404, detail="Faculty not found")

    if faculty.approval_status != "approved":
        raise HTTPException(status_code=403, detail=f"Faculty is not approved (status: {faculty.approval_status})")

    best_reading = pick_best_reading(payload.gps_readings)
    location_check = check_location(best_reading)
    face_result = verify_face(payload.face_embedding, faculty.face_embedding)

    if not location_check["allowed"]:
        status = "rejected_location"
    elif face_result["verified"] != "pass":
        status = "rejected_face"
    else:
        status = "present"  # "present" here just means "exit successfully logged"

    record = models.AttendanceRecord(
        faculty_id=faculty.id,
        timestamp=datetime.utcnow(),
        latitude=best_reading.latitude,
        longitude=best_reading.longitude,
        gps_accuracy=best_reading.accuracy,
        readings_used=len(payload.gps_readings),
        wifi_ssid=payload.wifi_ssid,
        face_match_score=face_result["score"],
        face_verified=face_result["verified"],
        status=status,
        notes=location_check["reason"],
        record_type="check_out",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    manager.broadcast_threadsafe({
        "event": "attendance",
        "data": {
            "record_id": record.id,
            "faculty_id": faculty.id,
            "faculty_name": faculty.name,
            "department": faculty.department,
            "record_type": "check_out",
            "status": status,
            "timestamp": record.timestamp.isoformat(),
            "face_match_score": face_result["score"],
            "distance_to_boundary_m": location_check["distance_to_boundary_m"],
        },
    })

    return schemas.CheckInResponse(
        status=status,
        reason=location_check["reason"] if status != "present" else face_result.get("reason"),
        distance_to_boundary_m=location_check["distance_to_boundary_m"],
        face_match_score=face_result["score"],
        gps_accuracy_used=best_reading.accuracy,
        record_id=record.id,
    )


@app.get("/api/leave-balance", response_model=schemas.LeaveBalanceOut)
def get_leave_balance(faculty_id: int, db: Session = Depends(get_db)):
    faculty = db.query(models.Faculty).filter(models.Faculty.id == faculty_id).first()
    if not faculty:
        raise HTTPException(status_code=404, detail="Faculty not found")

    b = get_or_create_leave_balance(db, faculty_id)

    return schemas.LeaveBalanceOut(
        faculty_id=b.faculty_id,
        semester_label=b.semester_label,
        casual_leave_total=b.casual_leave_total,
        casual_leave_used=b.casual_leave_used,
        casual_leave_remaining=b.casual_leave_total - b.casual_leave_used,
        working_days_total=b.working_days_total,
        working_days_attended=b.working_days_attended,
        working_days_remaining=max(0, b.working_days_total - b.working_days_attended),
        late_margin_total=b.late_margin_total_minutes,
        late_margin_used=b.late_margin_used_minutes,
        late_margin_remaining=max(0, b.late_margin_total_minutes - b.late_margin_used_minutes),
    )


@app.get("/api/salary", response_model=List[schemas.SalaryOut])
def get_salary_records(faculty_id: int, db: Session = Depends(get_db)):
    """
    Placeholder — no payroll system wired up yet. Returns whatever rows
    exist in salary_records for this faculty (admin dashboard would need
    to create these; nothing auto-generates them yet).
    """
    faculty = db.query(models.Faculty).filter(models.Faculty.id == faculty_id).first()
    if not faculty:
        raise HTTPException(status_code=404, detail="Faculty not found")

    records = (
        db.query(models.SalaryRecord)
        .filter(models.SalaryRecord.faculty_id == faculty_id)
        .order_by(models.SalaryRecord.created_at.desc())
        .all()
    )
    return [
        schemas.SalaryOut(
            id=r.id,
            faculty_id=r.faculty_id,
            month=r.month_label,
            amount=r.amount,
            status=r.status,
            pay_date=r.pay_date,
        )
        for r in records
    ]
