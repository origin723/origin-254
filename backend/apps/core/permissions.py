from rest_framework.permissions import BasePermission


class IsPlatformAdmin(BasePermission):
    message = "Administrator access required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_platform_admin)


class IsSupplier(BasePermission):
    message = "Only farmers can do this."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_supplier)


class IsBuyer(BasePermission):
    message = "Only food entrepreneurs and processors can do this."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_buyer)


class IsLogistics(BasePermission):
    message = "Only logistics partners can do this."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_logistics)


class IsVerified(BasePermission):
    message = "Your account must be verified by Origin 254 before you can do this."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.is_platform_admin or user.is_verified))
