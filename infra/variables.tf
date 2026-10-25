variable "project_id" {
  description = "Scaleway project `agenda` (SCW_DEFAULT_PROJECT_ID)."
  type        = string
}

variable "vm_zone" {
  description = "Zone of the VM and its volumes. nl-ams-1: the account has no DEV1 quota in fr-par."
  type        = string
  default     = "nl-ams-1"
}

variable "bucket_region" {
  description = "Region of the buckets: another region than the VM (offsite backups)."
  type        = string
  default     = "fr-par"
}

variable "admin_principals" {
  description = <<-EOT
    Principals kept on every bucket policy besides the scoped application, so that the
    operator (and Tofu itself) can still manage the buckets. IAM user IDs, as "user_id:<uuid>".
  EOT
  type        = list(string)
}
