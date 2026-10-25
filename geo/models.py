from django.contrib.gis.db import models
from django.contrib.postgres.fields import ArrayField


class Commune(models.Model):
    insee = models.CharField(primary_key=True, max_length=5)
    name = models.CharField(max_length=100)
    # The town hall, not the centroid: a centroid can sit on a mountain.
    chef_lieu = models.PointField(srid=4326)
    geom = models.MultiPolygonField(srid=4326)
    postal_codes = ArrayField(models.CharField(max_length=5), default=list)

    class Meta:
        ordering = ["insee"]

    def __str__(self) -> str:
        return f"{self.name} ({self.insee})"


class TravelTime(models.Model):
    """Car travel time between two covered communes' reference points. None: unreachable."""

    origin = models.ForeignKey(Commune, on_delete=models.CASCADE, related_name="+")
    destination = models.ForeignKey(Commune, on_delete=models.CASCADE, related_name="+")
    minutes = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["origin", "destination"], name="travel_time_pair")]


class CoveredArea(models.Model):
    insee = models.OneToOneField(Commune, primary_key=True, on_delete=models.CASCADE, db_column="insee")


class MatrixInfo(models.Model):
    """The loaded matrix's metadata. A single row."""

    version = models.CharField(max_length=100)
    origin_for_area = models.CharField(max_length=5)
    area_minutes = models.PositiveIntegerField()
    loaded_at = models.DateTimeField(auto_now=True)
