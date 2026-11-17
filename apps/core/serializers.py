from rest_framework import serializers

from .models import AuditLog, Location, MatchingConfig


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ("id", "name", "county", "sub_county", "address", "latitude", "longitude")


class AuditLogSerializer(serializers.ModelSerializer):
    actor = serializers.StringRelatedField()

    class Meta:
        model = AuditLog
        fields = ("id", "created_at", "actor", "action", "target_type", "target_id", "target_repr", "details",
                  "ip_address")


class MatchingConfigSerializer(serializers.ModelSerializer):
    normalised_weights = serializers.SerializerMethodField()

    class Meta:
        model = MatchingConfig
        exclude = ("id", "updated_by")
        read_only_fields = ("updated_at",)

    def get_normalised_weights(self, obj) -> dict:
        return obj.weights()

    def validate(self, attrs):
        weight_keys = [k for k in attrs if k.startswith("weight_")]
        for k in weight_keys:
            if attrs[k] < 0:
                raise serializers.ValidationError({k: "Weights cannot be negative."})
        return attrs
