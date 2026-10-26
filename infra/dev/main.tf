# The dev VM `agenda-dev`, in a project of its own (`agenda-dev`), apart from prod's.
#
# Its own root module and state (agenda/dev.tfstate): a `tofu apply` in infra/ never touches
# dev, and one here never touches prod. Run through infra/dev/tofu (README.md).
#
# - project `agenda-dev`, with the dev VM's SSH key (Scaleway puts the project's keys in
#   root's authorized_keys at boot);
# - the VM: DEV1-M, nl-ams-1, Debian with Determinate Nix (cloud-init), a 40 GB SBS root volume,
#   a flexible IPv4 (a dynamic one changes at each power-on, and the laptop's firewall admits
#   destinations one by one);
# - a security group: inbound SSH and ICMP only;
# - the IAM application `agenda-dev-ops` and its policy: read, start and stop the servers of
#   this project, nothing else. Its API key is created outside Tofu (README.md), so that the
#   secret is in neither the state nor the dev VM.
#
# Powered off between sessions (tools/dev down): only the volume and the IP bill then.

terraform {
  required_version = ">= 1.8"
  required_providers {
    scaleway = {
      source  = "scaleway/scaleway"
      version = ">= 2.60, < 3.0"
    }
  }
}

# The organisation comes from SCW_DEFAULT_ORGANIZATION_ID. infra/dev/tofu unsets
# SCW_DEFAULT_PROJECT_ID (prod's project): every resource names the dev project itself.
provider "scaleway" {
  zone   = var.zone
  region = var.region
}

resource "scaleway_account_project" "dev" {
  name        = "agenda-dev"
  description = "agenda: the dev and deploy VM only. Nothing of prod's lives here."
}

resource "scaleway_iam_ssh_key" "dev" {
  name       = "agenda-dev"
  project_id = scaleway_account_project.dev.id
  public_key = var.ssh_public_key
}

resource "scaleway_instance_security_group" "dev" {
  project_id              = scaleway_account_project.dev.id
  name                    = "agenda-dev"
  description             = "agenda dev VM: inbound SSH and ICMP only"
  zone                    = var.zone
  stateful                = true
  inbound_default_policy  = "drop"
  outbound_default_policy = "accept"
  tags                    = ["agenda", "dev"]

  inbound_rule {
    action   = "accept"
    protocol = "TCP"
    port     = 22
  }

  inbound_rule {
    action   = "accept"
    protocol = "ICMP"
  }
}

resource "scaleway_instance_ip" "dev" {
  project_id = scaleway_account_project.dev.id
  zone       = var.zone
  type       = "routed_ipv4"
  tags       = ["agenda", "dev"]
}

resource "scaleway_instance_server" "dev" {
  project_id        = scaleway_account_project.dev.id
  name              = "agenda-dev"
  zone              = var.zone
  type              = var.server_type
  image             = "debian_bookworm"
  ip_id             = scaleway_instance_ip.dev.id
  security_group_id = scaleway_instance_security_group.dev.id
  tags              = ["agenda", "dev"]

  root_volume {
    size_in_gb            = var.root_volume_gb
    volume_type           = "sbs_volume"
    sbs_iops              = 5000
    delete_on_termination = true
  }

  user_data = {
    cloud-init = file("${path.module}/cloud-init.yaml")
  }

  depends_on = [scaleway_iam_ssh_key.dev]

  lifecycle {
    prevent_destroy = true
    # Powered on and off by tools/dev, not by Tofu. cloud-init runs once, at the first boot;
    # Scaleway keeps the host key fingerprints in user_data (ssh-host-fingerprints).
    ignore_changes = [state, image, user_data]
  }
}

# --- The key tools/dev uses -----------------------------------------------------------------

resource "scaleway_iam_application" "dev_ops" {
  name        = "agenda-dev-ops"
  description = "agenda: read, start and stop the dev VM (project agenda-dev only)"
}

# No create, delete or update: the key can't make a server, a volume or an IP, change the
# VM's tags, user data or type, or reach anything outside this project.
resource "scaleway_iam_policy" "dev_ops" {
  name           = "agenda-dev-ops"
  description    = "Start, stop and read the servers of project agenda-dev"
  application_id = scaleway_iam_application.dev_ops.id

  rule {
    project_ids = [scaleway_account_project.dev.id]
    permission_set_names = [
      "InstancesServerList",
      "InstancesServerRead",
      "InstancesServerStart",
      "InstancesServerStop",
    ]
  }
}
