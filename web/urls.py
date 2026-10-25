from django.urls import path, re_path

from web import views

app_name = "web"
urlpatterns = [
    path("", views.agenda, name="agenda"),
    re_path(r"^evenements/(?P<slug>[a-z0-9-]+)-(?P<day>\d{4}-\d{2}-\d{2})/$", views.occurrence, name="occurrence"),
    path("a-propos/", views.about, name="about"),
    path("healthz", views.healthz, name="healthz"),
    path("sw.js", views.service_worker, name="service-worker"),
]
