variable "zone" {
  description = "Zone of the dev VM (the account's DEV1 quota is in nl-ams-1)."
  type        = string
  default     = "nl-ams-1"
}

variable "region" {
  type    = string
  default = "nl-ams"
}

variable "server_type" {
  description = "Commercial type. Changing it powers the VM off and on (Tofu does it)."
  type        = string
  default     = "DEV1-M"
}

variable "root_volume_gb" {
  description = "Root volume (SBS, 5K IOPS). It holds the Nix store: grow it, never shrink it."
  type        = number
  default     = 40
}

variable "ssh_public_key" {
  description = "Public half of .secrets/agenda_ed25519, root's key on the dev VM."
  type        = string
}
