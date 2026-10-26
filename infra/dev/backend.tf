# Same bucket as prod, another key: the two states never meet (../backend.tf).
terraform {
  backend "s3" {
    bucket = "agenda-tfstate"
    key    = "agenda/dev.tfstate"
    region = "fr-par"
    endpoints = {
      s3 = "https://s3.fr-par.scw.cloud"
    }
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}
