variable "region" {
  description = "AWS region for everything here."
  type        = string
  default     = "eu-central-1"
}

variable "name" {
  description = "Prefix for resource names. Bucket names add the account ID so they are globally unique."
  type        = string
  default     = "seweb-crm"
}

variable "public_base_url" {
  description = "Where the application is served. The sign-in callback is derived from it."
  type        = string
  default     = "https://crm.seweb.co"

  validation {
    condition     = startswith(var.public_base_url, "https://") && !endswith(var.public_base_url, "/")
    error_message = "Use an https address with no trailing slash."
  }
}

variable "backup_retention_days" {
  description = "How long an encrypted database backup is kept. This is the window in docs/data-retention.md: change both together."
  type        = number
  default     = 30

  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 365
    error_message = "Between 7 and 365 days."
  }
}

variable "cognito_domain_prefix" {
  description = "Prefix of the hosted sign-in page: https://<prefix>.auth.<region>.amazoncognito.com. Must be unique in the region."
  type        = string
  default     = "seweb-crm"
}

variable "deletion_protection" {
  description = "Refuse to delete the user pool and the buckets' contents. Turn off only to tear the environment down."
  type        = bool
  default     = true
}
