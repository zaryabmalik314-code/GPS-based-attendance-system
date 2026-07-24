from pydantic import BaseModel, field_serializer
from datetime import datetime, timezone
from typing import List, Optional


class GPSReading(BaseModel):
    latitude: float
    longitude: float
    accuracy: float  # meters


class CheckInRequest(BaseModel):
    faculty_id: int
    gps_readings: List[GPSReading]  # send 3-5 readings from frontend, backend picks best
    wifi_ssid: Optional[str] = None
    face_images: List[str]  # base64 candidate frames (from ~5s video, client-filtered for blur); backend picks the best one and embeds it
    captured_at: Optional[datetime] = None  # for offline-queued check-ins — see main.py validation


class CheckInResponse(BaseModel):
    status: str
    reason: Optional[str] = None
    distance_to_boundary_m: Optional[float] = None
    face_match_score: Optional[float] = None
    gps_accuracy_used: Optional[float] = None
    record_id: Optional[int] = None


class FacultyEnrollRequest(BaseModel):
    name: str
    email: str
    teacher_id: str
    department: Optional[str] = None
    face_images: List[str]  # base64 frames, one per angle bucket (left/center/right), max 3
    pin: str  # 4-6 digit PIN the teacher will use to log in


class FacultyOut(BaseModel):
    id: int
    name: str
    email: str
    teacher_id: str
    department: Optional[str] = None
    approval_status: str
    profile_photo: Optional[str] = None

    class Config:
        from_attributes = True


class UploadPhotoRequest(BaseModel):
    teacher_id: str
    pin: str
    photo_base64: str  # data URL or raw base64 string, e.g. "data:image/jpeg;base64,..."


class UploadPhotoResponse(BaseModel):
    status: str  # "ok" | "invalid_credentials" | "not_approved" | "too_large"
    faculty: Optional[FacultyOut] = None


class LoginRequest(BaseModel):
    teacher_id: str
    pin: str


class LoginResponse(BaseModel):
    status: str  # "approved" | "pending" | "rejected" | "invalid_credentials"
    faculty: Optional[FacultyOut] = None


class ReEnrollFaceRequest(BaseModel):
    teacher_id: str
    pin: str
    face_images: List[str]  # base64 frames, one per angle bucket, replaces all stored embeddings


class ReEnrollFaceResponse(BaseModel):
    status: str  # "ok" | "invalid_credentials" | "not_approved"
    faculty: Optional[FacultyOut] = None


class ApprovalRequest(BaseModel):
    approval_status: str  # "approved" | "rejected"


class CheckOutRequest(BaseModel):
    faculty_id: int
    gps_readings: List[GPSReading]
    wifi_ssid: Optional[str] = None
    face_images: List[str]  # base64 candidate frames, same as check-in
    captured_at: Optional[datetime] = None  # for offline-queued check-outs


class AttendanceOut(BaseModel):
    id: int
    faculty_id: int
    faculty_name: Optional[str] = None
    department: Optional[str] = None
    timestamp: datetime
    latitude: float
    longitude: float
    gps_accuracy: float
    wifi_ssid: Optional[str] = None
    face_match_score: float
    status: str
    record_type: str
    flagged_suspicious: bool = False
    flag_reason: Optional[str] = None

    # DB timestamps are stored naive (no tzinfo) but are always UTC (written
    # via datetime.utcnow()). Without an explicit offset, clients parse the
    # string as their OWN local time instead of UTC, silently shifting every
    # displayed time. Stamp the UTC offset on the way out so it's unambiguous.
    @field_serializer('timestamp')
    def serialize_timestamp(self, dt: datetime, _info):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    class Config:
        from_attributes = True


class LeaveBalanceOut(BaseModel):
    faculty_id: int
    semester_label: str
    casual_leave_total: int
    casual_leave_used: int
    casual_leave_remaining: int
    working_days_total: int
    working_days_attended: int
    working_days_remaining: int
    late_margin_total: int
    late_margin_used: int
    late_margin_remaining: int

    class Config:
        from_attributes = True


class SalaryOut(BaseModel):
    id: int
    faculty_id: int
    month: str
    amount: Optional[float] = None
    status: str
    pay_date: Optional[datetime] = None

    class Config:
        from_attributes = True


class AdminBootstrapRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


class AdminLoginRequest(BaseModel):
    email: str
    password: str


class AdminOut(BaseModel):
    name: str
    email: str


class AdminLoginResponse(BaseModel):
    access_token: str
    admin: AdminOut
