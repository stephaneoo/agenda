output "project_id" {
  value = scaleway_account_project.dev.id
}

output "server_id" {
  value = scaleway_instance_server.dev.id
}

output "application_id" {
  description = "agenda-dev-ops: its API key is created outside Tofu (README.md)."
  value       = scaleway_iam_application.dev_ops.id
}

output "public_ip" {
  description = "The dev VM's flexible IPv4 (.secrets/dev-ip)."
  value       = scaleway_instance_ip.dev.address
}
