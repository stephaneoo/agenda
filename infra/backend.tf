# The state lives only in the versioned bucket agenda-tfstate (fr-par), created once with the
# scw CLI (see README.md). Never `tofu state pull` it or copy it to the laptop: it holds the
# IAM secret keys. Credentials come from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, which
# infra/tofu sets from the SCW_* variables.
terraform {
  backend "s3" {
    bucket = "agenda-tfstate"
    key    = "agenda/prod.tfstate"
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
