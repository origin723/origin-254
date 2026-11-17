from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts import api as accounts
from apps.catalog import api as catalog
from apps.comms import api as comms
from apps.core import api as core
from apps.orders import api as orders
from apps.payments import api as payments
from apps.reviews import api as reviews
from apps.sourcing import api as sourcing

router = DefaultRouter()
router.register("documents", accounts.DocumentViewSet, basename="document")
router.register("suppliers", accounts.SupplierDirectoryViewSet, basename="supplier")
router.register("categories", catalog.CategoryViewSet)
router.register("products", catalog.ProductViewSet)
router.register("varieties", catalog.VarietyViewSet)
router.register("locations", catalog.LocationViewSet)
router.register("inventory", catalog.InventoryViewSet, basename="inventory")
router.register("sourcing-requests", sourcing.SourcingRequestViewSet, basename="sourcing-request")
router.register("matches", sourcing.MatchViewSet, basename="match")
router.register("quotations", sourcing.QuotationViewSet, basename="quotation")
router.register("orders", orders.OrderViewSet, basename="order")
router.register("order-lines", orders.OrderLineViewSet, basename="order-line")
router.register("deliveries", orders.DeliveryViewSet, basename="delivery")
router.register("inspections", orders.InspectionViewSet, basename="inspection")
router.register("disputes", orders.DisputeViewSet, basename="dispute")
router.register("history", orders.SaleHistoryViewSet, basename="history")
router.register("payments", payments.PaymentViewSet, basename="payment")
router.register("payouts", payments.PayoutViewSet, basename="payout")
router.register("conversations", comms.ConversationViewSet, basename="conversation")
router.register("notifications", comms.NotificationViewSet, basename="notification")
router.register("reviews", reviews.ReviewViewSet)
router.register("admin/users", accounts.AdminUserViewSet, basename="admin-user")
router.register("admin/suppliers", accounts.AdminSupplierViewSet, basename="admin-supplier")
router.register("admin/buyers", accounts.AdminBuyerViewSet, basename="admin-buyer")
router.register("admin/logistics", accounts.AdminLogisticsViewSet, basename="admin-logistics")
router.register("admin/audit-logs", core.AuditLogViewSet, basename="audit-log")

api_v1 = [
    path("auth/register/", accounts.RegisterView.as_view(), name="register"),
    path("auth/token/", TokenObtainPairView.as_view(), name="token"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("auth/me/", accounts.MeView.as_view(), name="me"),
    path("auth/me/profile/", accounts.MyProfileView.as_view(), name="my-profile"),
    path("admin/analytics/", core.AnalyticsView.as_view(), name="analytics"),
    path("admin/matching-config/", core.MatchingConfigView.as_view(), name="matching-config"),
    path("", include(router.urls)),
]

admin.site.site_header = "Origin 254 administration"
admin.site.site_title = "Origin 254 admin"

urlpatterns = [
    path("health/", lambda r: JsonResponse({"status": "ok"}), name="health"),
    path("django-admin/", admin.site.urls),
    path("api/v1/", include(api_v1)),
    path("payments/", include("apps.payments.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("", include("apps.web.urls")),
]
