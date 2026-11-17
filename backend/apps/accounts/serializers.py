from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from apps.core.models import Location
from apps.core.serializers import LocationSerializer

from .models import (BuyerProfile, LogisticsProfile, Role, SupplierProfile, User, VerificationDocument,
                     VerificationStatus)
from .services import initial_verification_fields, profile_model_for_role

PROFILE_READONLY = ("verification_status", "verification_notes", "verified_at", "created_at")


class SupplierProfileSerializer(serializers.ModelSerializer):
    location = LocationSerializer(read_only=True)
    location_id = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all(), source="location",
                                                     write_only=True)
    supplier_type = serializers.CharField(source="user.role", read_only=True)

    class Meta:
        model = SupplierProfile
        fields = ("id", "business_name", "supplier_type", "registration_number", "location", "location_id",
                  "description", "farm_size_acres", "member_count", "can_deliver", "delivery_radius_km",
                  "certifications", "reliability_score", "completed_orders", "average_rating", "on_time_rate",
                  "inspection_pass_rate", *PROFILE_READONLY)
        read_only_fields = ("reliability_score", "completed_orders", "average_rating", "on_time_rate",
                            "inspection_pass_rate", *PROFILE_READONLY)


class BuyerProfileSerializer(serializers.ModelSerializer):
    location = LocationSerializer(read_only=True)
    location_id = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all(), source="location",
                                                     write_only=True)

    class Meta:
        model = BuyerProfile
        fields = ("id", "business_name", "business_type", "registration_number", "location", "location_id",
                  "description", "monthly_volume_kg", *PROFILE_READONLY)
        read_only_fields = PROFILE_READONLY


class LogisticsProfileSerializer(serializers.ModelSerializer):
    location = LocationSerializer(read_only=True)
    location_id = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all(), source="location",
                                                     write_only=True)

    class Meta:
        model = LogisticsProfile
        fields = ("id", "business_name", "registration_number", "location", "location_id", "description",
                  "vehicle_capacity_kg", "refrigerated", "coverage_counties", *PROFILE_READONLY)
        read_only_fields = PROFILE_READONLY


PROFILE_SERIALIZERS = {SupplierProfile: SupplierProfileSerializer, BuyerProfile: BuyerProfileSerializer,
                       LogisticsProfile: LogisticsProfileSerializer}


def profile_serializer_for(user):
    model = profile_model_for_role(user.role)
    return PROFILE_SERIALIZERS.get(model)


class UserSerializer(serializers.ModelSerializer):
    profile = serializers.SerializerMethodField()
    is_verified = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "phone", "role", "sms_opt_in",
                  "whatsapp_opt_in", "is_verified", "profile", "date_joined")
        read_only_fields = ("id", "username", "role", "is_verified", "date_joined")

    def get_profile(self, user) -> dict | None:
        profile, ser = user.profile, profile_serializer_for(user)
        return ser(profile, context=self.context).data if profile and ser else None


class RegisterSerializer(serializers.Serializer):
    """Creates the user and their role profile in one step. New profiles start PENDING verification."""

    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=16, required=False, allow_blank=True)
    role = serializers.ChoiceField(choices=[c for c in Role.choices if c[0] != Role.ADMIN])
    profile = serializers.DictField()

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is taken.")
        return value

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value.lower()

    def validate(self, attrs):
        user = User(username=attrs["username"], email=attrs["email"])
        validate_password(attrs["password"], user)
        model = profile_model_for_role(attrs["role"])
        ser = PROFILE_SERIALIZERS[model](data=attrs["profile"])
        ser.is_valid(raise_exception=True)
        attrs["_profile_serializer"] = ser
        return attrs

    @transaction.atomic
    def create(self, data):
        profile_ser = data.pop("_profile_serializer")
        data.pop("profile")
        password = data.pop("password")
        user = User(**data)
        user.set_password(password)
        user.full_clean(exclude=["password"])
        user.save()
        profile_ser.save(user=user, **initial_verification_fields())
        return user


class DocumentSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = VerificationDocument
        fields = ("id", "doc_type", "file", "original_name", "status", "created_at", "download_url")
        read_only_fields = ("original_name", "status", "created_at")
        extra_kwargs = {"file": {"write_only": True}}

    def validate_file(self, f):
        if f.size > settings.MAX_DOCUMENT_SIZE_MB * 1024 * 1024:
            raise serializers.ValidationError(f"Files must be under {settings.MAX_DOCUMENT_SIZE_MB} MB.")
        return f

    def get_download_url(self, obj) -> str:
        return f"/api/v1/documents/{obj.pk}/download/"


class VerificationDecisionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=VerificationStatus.choices)
    notes = serializers.CharField(required=False, allow_blank=True)


class PublicSupplierSerializer(serializers.ModelSerializer):
    """What buyers see about a supplier in the directory."""

    location = LocationSerializer(read_only=True)
    supplier_type = serializers.CharField(source="user.get_role_display", read_only=True)
    user_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = SupplierProfile
        fields = ("id", "user_id", "business_name", "supplier_type", "location", "description", "certifications",
                  "can_deliver", "delivery_radius_km", "member_count", "reliability_score", "completed_orders",
                  "average_rating", "verification_status")
