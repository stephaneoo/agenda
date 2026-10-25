output "public_ip" {
  description = "The prod VM's flexible IPv4."
  value       = scaleway_instance_ip.prod.address
}

output "server_id" {
  value = scaleway_instance_server.prod.id
}

output "root_volume_id" {
  description = "Root disk: /dev/disk/by-id/scsi-0SCW_sbs_volume-<id> on the VM."
  value       = scaleway_instance_server.prod.root_volume[0].volume_id
}

output "zfs_volume_id" {
  description = "ZFS pool disk: /dev/disk/by-id/scsi-0SCW_sbs_volume-<id> on the VM."
  value       = scaleway_block_volume.zfs.id
}

output "wals_endpoint" {
  value = "https://s3.${var.bucket_region}.scw.cloud/${scaleway_object_bucket.wals.name}"
}

output "assets_endpoint" {
  value = "https://s3.${var.bucket_region}.scw.cloud/${scaleway_object_bucket.assets.name}"
}

# Read only by piping `tofu output -json` straight into sops on the dev VM (plan, Task 14).
output "pgbackrest_access_key" {
  value     = scaleway_iam_api_key.pgbackrest.access_key
  sensitive = true
}

output "pgbackrest_secret_key" {
  value     = scaleway_iam_api_key.pgbackrest.secret_key
  sensitive = true
}

output "django_storage_access_key" {
  value     = scaleway_iam_api_key.django_storage.access_key
  sensitive = true
}

output "django_storage_secret_key" {
  value     = scaleway_iam_api_key.django_storage.secret_key
  sensitive = true
}
