output "s3_region" {
  description = "S3_REGION in /etc/seweb-crm/env"
  value       = var.region
}

output "files_bucket" {
  description = "S3_BUCKET in /etc/seweb-crm/env"
  value       = aws_s3_bucket.files.bucket
}

output "backups_uri" {
  description = "BACKUP_S3_URI in /etc/seweb-crm/deploy.conf"
  value       = "s3://${aws_s3_bucket.backups.bucket}/crm"
}

output "app_iam_user" {
  description = "Create an access key for this user: S3_ACCESS_KEY and S3_SECRET_KEY in /etc/seweb-crm/env"
  value       = aws_iam_user.app.name
}

output "backup_iam_user" {
  description = "Create an access key for this user: /etc/seweb-crm/backup-aws.env"
  value       = aws_iam_user.backup.name
}

output "oidc_issuer" {
  description = "OIDC_ISSUER in /etc/seweb-crm/env"
  value       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
}

output "oidc_client_id" {
  description = "OIDC_CLIENT_ID in /etc/seweb-crm/env"
  value       = aws_cognito_user_pool_client.app.id
}

output "oidc_client_secret" {
  description = "OIDC_CLIENT_SECRET in /etc/seweb-crm/env. Read it with: terraform output -raw oidc_client_secret"
  value       = aws_cognito_user_pool_client.app.client_secret
  sensitive   = true
}

output "user_pool_id" {
  description = "For creating users: aws cognito-idp admin-create-user --user-pool-id ..."
  value       = aws_cognito_user_pool.main.id
}

output "sign_in_page" {
  description = "The hosted sign-in page's address"
  value       = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.region}.amazoncognito.com"
}
