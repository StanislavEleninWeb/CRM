# Sign-in for the application. People are created by an administrator; nobody can register themselves.

resource "aws_cognito_user_pool" "main" {
  name                     = var.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = var.deletion_protection ? "ACTIVE" : "INACTIVE"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # A second factor is required for everyone. The application cannot see whether one was used
  # (Cognito does not say so in the sign-in token), so this setting is the enforcement.
  mfa_configuration = "ON"
  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  user_attribute_update_settings {
    # A changed address is not used until it has been verified.
    attributes_require_verification_before_update = ["email"]
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 3
      max_length = 254
    }
  }

  lifecycle {
    # Cognito does not allow a pool's schema to be changed after creation.
    ignore_changes = [schema]
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = var.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_cognito_user_pool_client" "app" {
  name         = "${var.name}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${var.public_base_url}/api/v1/auth/callback"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]

  # The same answer whether or not an address has an account.
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  # The application keeps its own session and does not use Cognito's tokens after sign-in.
  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 1
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}
