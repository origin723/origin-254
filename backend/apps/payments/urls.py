from django.urls import path

from . import views

urlpatterns = [
    path("mpesa/stk/<str:token>/", views.stk_callback, name="mpesa_stk_callback"),
    path("mpesa/b2c-result/<str:token>/", views.b2c_result, name="mpesa_b2c_result"),
    path("mpesa/b2c-timeout/<str:token>/", views.b2c_timeout, name="mpesa_b2c_timeout"),
]
