import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import FileExtensionValidator, RegexValidator
from django.db import models

from apps.core.models import Location, TimeStampedModel

phone_validator = RegexValidator(r"^\+?[0-9]{9,15}$", "Enter a phone number like +254712345678.")


class Role(models.TextChoices):
    FARMER = "farmer", "Farmer / producer"
    PROCESSOR = "processor", "Food entrepreneur / processor"
    LOGISTICS = "logistics", "Logistics & delivery partner"
    ADMIN = "admin", "Platform administrator"


SUPPLIER_ROLES = {Role.FARMER}
BUYER_ROLES = {Role.PROCESSOR}


class VerificationStatus(models.TextChoices):
    PENDING = "pending", "Pending review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    SUSPENDED = "suspended", "Suspended"


class User(AbstractUser):
    role = models.CharField(max_length=20, choices=Role.choices, db_index=True)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=16, blank=True, validators=[phone_validator])
    whatsapp_opt_in = models.BooleanField(default=False)
    sms_opt_in = models.BooleanField(default=True)

    REQUIRED_FIELDS = ["email", "role"]

    @property
    def is_platform_admin(self):
        return self.is_superuser or self.role == Role.ADMIN

    @property
    def is_supplier(self):
        return self.role in SUPPLIER_ROLES

    @property
    def is_buyer(self):
        return self.role in BUYER_ROLES

    @property
    def is_logistics(self):
        return self.role == Role.LOGISTICS

    @property
    def profile(self):
        """The role-specific profile (supplier, buyer or logistics) or None."""
        for attr in ("supplier_profile", "buyer_profile", "logistics_profile"):
            try:
                return getattr(self, attr)
            except models.ObjectDoesNotExist:
                continue
        return None

    @property
    def is_verified(self):
        if self.is_platform_admin:
            return True
        profile = self.profile
        return bool(profile and profile.verification_status == VerificationStatus.APPROVED)

    @property
    def display_name(self):
        profile = self.profile
        return getattr(profile, "business_name", None) or self.get_full_name() or self.username


class VerifiableProfile(TimeStampedModel):
    business_name = models.CharField(max_length=160)
    registration_number = models.CharField(max_length=60, blank=True,
                                           help_text="Business / cooperative registration or KRA PIN.")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    description = models.TextField(blank=True)
    verification_status = models.CharField(max_length=12, choices=VerificationStatus.choices,
                                           default=VerificationStatus.PENDING, db_index=True)
    verification_notes = models.TextField(blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")

    class Meta:
        abstract = True

    def __str__(self):
        return self.business_name

    @property
    def is_approved(self):
        return self.verification_status == VerificationStatus.APPROVED


class SupplierProfile(VerifiableProfile):
    """Farmers: anyone who supplies produce."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="supplier_profile")
    farm_size_acres = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    member_count = models.PositiveIntegerField(null=True, blank=True, help_text="Cooperatives: number of members.")
    can_deliver = models.BooleanField(default=False)
    delivery_radius_km = models.PositiveIntegerField(default=0)
    certifications = models.CharField(max_length=255, blank=True, help_text="e.g. KEBS, GlobalG.A.P., organic")

    # Performance metrics, recalculated from completed transactions.
    reliability_score = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    completed_orders = models.PositiveIntegerField(default=0)
    average_rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    on_time_rate = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    inspection_pass_rate = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)

    @property
    def supplier_type(self):
        return self.user.get_role_display()

    def effective_reliability(self, default=Decimal("0.6")):
        return self.reliability_score if self.reliability_score is not None else default


class BuyerProfile(VerifiableProfile):
    """Food entrepreneurs and processors."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="buyer_profile")
    business_type = models.CharField(max_length=120, blank=True, help_text="e.g. jam producer, juice processor")
    monthly_volume_kg = models.PositiveIntegerField(null=True, blank=True)


class LogisticsProfile(VerifiableProfile):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="logistics_profile")
    vehicle_capacity_kg = models.PositiveIntegerField(default=0)
    refrigerated = models.BooleanField(default=False)
    coverage_counties = models.CharField(max_length=255, blank=True, help_text="Comma separated")


def document_upload_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    return f"verification/{instance.owner_id}/{uuid.uuid4().hex}.{ext}"


class VerificationDocument(TimeStampedModel):
    class DocType(models.TextChoices):
        NATIONAL_ID = "national_id", "National ID"
        BUSINESS_REG = "business_registration", "Business registration certificate"
        KRA_PIN = "kra_pin", "KRA PIN certificate"
        COOP_CERT = "cooperative_certificate", "Cooperative registration certificate"
        PERMIT = "permit", "Licence / permit"
        QUALITY_CERT = "quality_certificate", "Quality / food safety certificate"
        OTHER = "other", "Other"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=40, choices=DocType.choices)
    file = models.FileField(upload_to=document_upload_path,
                            validators=[FileExtensionValidator(settings.ALLOWED_DOCUMENT_EXTENSIONS)])
    original_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, choices=VerificationStatus.choices, default=VerificationStatus.PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")

    def __str__(self):
        return f"{self.get_doc_type_display()} — {self.owner}"
