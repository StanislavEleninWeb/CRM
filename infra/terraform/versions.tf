terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }

  # State holds the Cognito client secret, so it is kept in an encrypted bucket, never in the
  # repository. The bucket is named at init time:  terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "seweb-crm"
      ManagedBy = "terraform"
    }
  }
}
