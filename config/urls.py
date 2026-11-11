from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.authtoken.views import obtain_auth_token

from apps.core.auth import RegisterView
from apps.core.views import health

api = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/token/", obtain_auth_token, name="token"),
    path("", include("apps.catalog.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", health, name="health"),
    path("api/", include(api)),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
