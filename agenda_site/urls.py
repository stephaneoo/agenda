from django.contrib import admin
from django.urls import include, path, re_path

from api.problems import not_found as api_not_found
from api.v1 import api as api_v1

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api_v1.urls),
    re_path(r"^api/v1/", api_not_found),  # anything else under /api/v1/ is a problem+json 404
    path("", include("web.urls")),
]
