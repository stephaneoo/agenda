# Scaleway resources for agenda v1 (plan, Task 13). Cost: see the plan's estimate.
#
# - nl-ams-1: the VM (DEV1-M, x86_64, Debian only for the nixos-anywhere bootstrap), its
#   20 GB root volume, a 50 GB block volume for the ZFS pool `tank`, a flexible IPv4.
# - fr-par: the buckets agenda-wals (pgBackRest) and agenda-assets (django-storages, empty
#   in v1), offsite from the VM. The state bucket agenda-tfstate is created by hand (README).
# - IAM: the applications `pgbackrest` and `django-storage`, each with one API key. They get
#   no IAM policy at all: the only thing they may touch is their own bucket, granted by that
#   bucket's policy.

terraform {
  required_version = ">= 1.8"
  required_providers {
    scaleway = {
      source  = "scaleway/scaleway"
      version = ">= 2.60, < 3.0"
    }
  }
}

provider "scaleway" {
  project_id = var.project_id
  zone       = var.vm_zone
  region     = var.bucket_region
}

# --- VM -------------------------------------------------------------------------------------

resource "scaleway_instance_ip" "prod" {
  zone = var.vm_zone
  type = "routed_ipv4"
  tags = ["agenda", "prod"]

  lifecycle {
    prevent_destroy = true
  }
}

resource "scaleway_block_volume" "zfs" {
  name       = "agenda-zfs"
  zone       = var.vm_zone
  size_in_gb = 50
  iops       = 5000
  tags       = ["agenda", "prod", "zfs"]

  lifecycle {
    prevent_destroy = true
  }
}

resource "scaleway_instance_server" "prod" {
  name  = "agenda-prod"
  zone  = var.vm_zone
  type  = "DEV1-M"
  image = "debian_bookworm"
  ip_id = scaleway_instance_ip.prod.id
  tags  = ["agenda", "prod"]

  root_volume {
    size_in_gb            = 20
    volume_type           = "sbs_volume"
    sbs_iops              = 5000
    delete_on_termination = false
  }

  additional_volume_ids = [scaleway_block_volume.zfs.id]

  lifecycle {
    prevent_destroy = true
    # nixos-anywhere replaces Debian in place: the image is only the bootstrap.
    ignore_changes = [image, user_data]
  }
}

# --- Buckets --------------------------------------------------------------------------------

resource "scaleway_object_bucket" "wals" {
  name   = "agenda-wals"
  region = var.bucket_region
  tags = {
    project = "agenda"
    role    = "pgbackrest"
  }

  versioning {
    enabled = false
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "scaleway_object_bucket_acl" "wals" {
  bucket = scaleway_object_bucket.wals.id
  region = var.bucket_region
  acl    = "private"
}

resource "scaleway_object_bucket" "assets" {
  name   = "agenda-assets"
  region = var.bucket_region
  tags = {
    project = "agenda"
    role    = "django-storage"
  }

  versioning {
    enabled = true
  }

  # Spec: versions kept for 6 months.
  lifecycle_rule {
    id      = "noncurrent-180-days"
    enabled = true

    noncurrent_version_expiration {
      noncurrent_days = 180
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "scaleway_object_bucket_acl" "assets" {
  bucket = scaleway_object_bucket.assets.id
  region = var.bucket_region
  acl    = "private"
}

# --- Scoped IAM applications ----------------------------------------------------------------

resource "scaleway_iam_application" "pgbackrest" {
  name        = "pgbackrest"
  description = "agenda: pgBackRest repository in agenda-wals only (bucket policy)"
}

resource "scaleway_iam_api_key" "pgbackrest" {
  application_id     = scaleway_iam_application.pgbackrest.id
  description        = "agenda prod host, pgBackRest"
  default_project_id = var.project_id

  # The organisation gives every key a one-year expiry (2027-10-25 for these): rotate before
  # then (docs/ops/RUNBOOK.md). Ignored here so that Tofu doesn't replace the keys.
  lifecycle {
    ignore_changes = [expires_at]
  }
}

resource "scaleway_iam_application" "django_storage" {
  name        = "django-storage"
  description = "agenda: media files in agenda-assets only (bucket policy)"
}

resource "scaleway_iam_api_key" "django_storage" {
  application_id     = scaleway_iam_application.django_storage.id
  description        = "agenda prod host, django-storages"
  default_project_id = var.project_id

  lifecycle {
    ignore_changes = [expires_at]
  }
}

# Scaleway needs both: an IAM policy lets the application use Object Storage in the project at
# all, and each bucket's policy then admits only the principals it names. Every bucket in the
# project has one (agenda-tfstate included, below), so each key reaches its own bucket only.
resource "scaleway_iam_policy" "object_storage" {
  for_each = {
    pgbackrest     = scaleway_iam_application.pgbackrest.id
    django-storage = scaleway_iam_application.django_storage.id
  }
  name           = "agenda-${each.key}-object-storage"
  description    = "Object Storage in the agenda project; narrowed to one bucket by its bucket policy"
  application_id = each.value

  rule {
    project_ids = [var.project_id]
    permission_set_names = [
      "ObjectStorageBucketsRead",
      "ObjectStorageObjectsRead",
      "ObjectStorageObjectsWrite",
      "ObjectStorageObjectsDelete",
    ]
  }
}

# A bucket policy denies every principal it doesn't name, the project owner included: the
# admin principals stay on each one.
locals {
  bucket_policies = {
    wals = {
      bucket      = scaleway_object_bucket.wals
      application = scaleway_iam_application.pgbackrest
    }
    assets = {
      bucket      = scaleway_object_bucket.assets
      application = scaleway_iam_application.django_storage
    }
  }
}

resource "scaleway_object_bucket_policy" "scoped" {
  for_each = local.bucket_policies
  bucket   = each.value.bucket.id
  region   = var.bucket_region
  policy = jsonencode({
    Version = "2023-04-17"
    Id      = "agenda-${each.key}"
    Statement = [
      {
        Sid       = "Admins"
        Effect    = "Allow"
        Principal = { SCW = var.admin_principals }
        Action    = ["s3:*"]
        Resource  = [each.value.bucket.name, "${each.value.bucket.name}/*"]
      },
      {
        Sid       = "App"
        Effect    = "Allow"
        Principal = { SCW = "application_id:${each.value.application.id}" }
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation",
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucketMultipartUploads",
          "s3:ListMultipartUploadParts",
          "s3:AbortMultipartUpload",
        ]
        Resource = [each.value.bucket.name, "${each.value.bucket.name}/*"]
      },
    ]
  })
}

# The state bucket (created by hand, README.md): the admins only, so that neither application
# key can read the state and the secrets in it.
resource "scaleway_object_bucket_policy" "tfstate" {
  bucket = "agenda-tfstate"
  region = var.bucket_region
  policy = jsonencode({
    Version = "2023-04-17"
    Id      = "agenda-tfstate"
    Statement = [
      {
        Sid       = "Admins"
        Effect    = "Allow"
        Principal = { SCW = var.admin_principals }
        Action    = ["s3:*"]
        Resource  = ["agenda-tfstate", "agenda-tfstate/*"]
      },
    ]
  })
}
