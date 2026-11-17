from django.db import transaction
from django.utils import timezone

from apps.comms.notify import notify
from apps.comms.models import Notification
from apps.core.audit import log_action
from apps.core.exceptions import WorkflowError

from .models import BuyerProfile, LogisticsProfile, Role, SupplierProfile, VerificationStatus


def profile_model_for_role(role):
    if role == Role.FARMER:
        return SupplierProfile
    if role == Role.PROCESSOR:
        return BuyerProfile
    if role == Role.LOGISTICS:
        return LogisticsProfile
    return None


def initial_verification_status():
    """Status for a newly registered profile (see settings.REQUIRE_ACCOUNT_VERIFICATION)."""
    from django.conf import settings
    return VerificationStatus.PENDING if settings.REQUIRE_ACCOUNT_VERIFICATION else VerificationStatus.APPROVED


def initial_verification_fields():
    status = initial_verification_status()
    return {"verification_status": status,
            "verified_at": timezone.now() if status == VerificationStatus.APPROVED else None,
            "verification_notes": "Approved automatically at registration"
            if status == VerificationStatus.APPROVED else ""}


@transaction.atomic
def set_verification(profile, admin, status, notes=""):
    """Approve, reject or suspend a supplier / buyer / logistics profile."""
    if not admin.is_platform_admin:
        raise WorkflowError("Only administrators can verify accounts.", "permission_denied")
    if status not in VerificationStatus.values:
        raise WorkflowError(f"Unknown verification status '{status}'.", "invalid_status")
    previous = profile.verification_status
    profile.verification_status = status
    profile.verification_notes = notes
    profile.verified_by = admin
    profile.verified_at = timezone.now() if status == VerificationStatus.APPROVED else profile.verified_at
    profile.save(update_fields=["verification_status", "verification_notes", "verified_by", "verified_at",
                                "updated_at"])
    if status == VerificationStatus.APPROVED:
        profile.user.documents.filter(status=VerificationStatus.PENDING).update(
            status=VerificationStatus.APPROVED, reviewed_by=admin)
    log_action(admin, f"verification.{status}", profile, previous=previous, notes=notes)

    messages = {
        VerificationStatus.APPROVED: ("Your Origin 254 account is verified",
                                      "You can now take part in verified sourcing."),
        VerificationStatus.REJECTED: ("Your verification was not approved", notes or "Please contact support."),
        VerificationStatus.SUSPENDED: ("Your account has been suspended", notes or "Please contact support."),
    }
    if status in messages:
        title, body = messages[status]
        notify(profile.user, title, body, kind=Notification.Kind.ACCOUNT, link="/app/")
    return profile
