from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "web"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("login/", auth_views.LoginView.as_view(template_name="web/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("register/", views.register, name="register"),
    path("app/", views.dashboard, name="dashboard"),
    path("app/profile/", views.profile, name="profile"),
    path("app/documents/<int:pk>/", views.document_download, name="document_download"),
    # Buyer
    path("app/requests/", views.request_list, name="request_list"),
    path("app/suppliers/nearest/", views.nearest_suppliers_view, name="nearest_suppliers"),
    path("app/requests/new/", views.request_new, name="request_new"),
    path("app/requests/<int:pk>/", views.request_detail, name="request_detail"),
    path("app/requests/<int:pk>/<str:action>/", views.request_action, name="request_action"),
    path("app/quotations/<int:pk>/buyer/<str:action>/", views.quotation_buyer_action, name="quotation_buyer_action"),
    # Supplier
    path("app/inventory/", views.inventory, name="inventory"),
    path("app/inventory/<int:pk>/photos/", views.inventory_photo_upload, name="inventory_photo_upload"),
    path("app/inventory/<int:pk>/<str:action>/", views.inventory_action, name="inventory_action"),
    path("app/photos/<int:pk>/", views.photo_view, name="photo"),
    path("app/photos/<int:pk>/delete/", views.inventory_photo_delete, name="photo_delete"),
    path("app/market/", views.market, name="market"),
    path("app/opportunities/", views.opportunities, name="opportunities"),
    path("app/opportunities/<int:pk>/", views.opportunity_detail, name="opportunity_detail"),
    path("app/opportunities/<int:pk>/respond/", views.opportunity_respond, name="opportunity_respond"),
    path("app/opportunities/<int:pk>/message/", views.opportunity_message, name="opportunity_message"),
    path("app/quotations/", views.quotation_list, name="quotation_list"),
    path("app/quotations/<int:pk>/", views.quotation_detail, name="quotation_detail"),
    path("app/quotations/<int:pk>/supplier/<str:action>/", views.quotation_supplier_action,
         name="quotation_supplier_action"),
    # Orders & delivery
    path("app/orders/", views.order_list, name="order_list"),
    path("app/orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("app/orders/<int:pk>/<str:action>/", views.order_action, name="order_action"),
    path("app/order-lines/<int:pk>/<str:action>/", views.order_line_action, name="order_line_action"),
    path("app/deliveries/", views.deliveries, name="deliveries"),
    path("app/history/", views.history, name="history"),
    path("app/payments/", views.payments, name="payments"),
    path("app/payments/pay/", views.payments_pay, name="payments_pay"),
    # Comms
    path("app/messages/", views.conversations, name="conversations"),
    path("app/messages/<int:pk>/", views.conversation, name="conversation"),
    path("app/notifications/", views.notifications, name="notifications"),
    path("app/notifications/<int:pk>/", views.notification_open, name="notification_open"),
    # Admin
    path("app/admin/", views.admin_dashboard, name="admin_dashboard"),
    path("app/admin/verification/", views.admin_verification, name="admin_verification"),
    path("app/admin/verification/<str:kind>/<int:pk>/", views.admin_verify, name="admin_verify"),
    path("app/admin/requests/", views.admin_requests, name="admin_requests"),
    path("app/admin/requests/<int:pk>/", views.admin_request_detail, name="admin_request_detail"),
    path("app/admin/disputes/", views.admin_disputes, name="admin_disputes"),
    path("app/admin/disputes/<int:pk>/", views.admin_dispute_detail, name="admin_dispute_detail"),
    path("app/admin/disputes/<int:pk>/resolve/", views.admin_dispute_resolve, name="admin_dispute_resolve"),
    path("app/admin/config/", views.admin_config, name="admin_config"),
    path("app/admin/audit/", views.admin_audit, name="admin_audit"),
    path("app/admin/payments/", views.admin_payments, name="admin_payments"),
]
