data "aws_caller_identity" "current" {}

locals {
  files_bucket   = "${var.name}-files-${data.aws_caller_identity.current.account_id}"
  backups_bucket = "${var.name}-backups-${data.aws_caller_identity.current.account_id}"
}

# --- uploaded prospect lists and attachments --------------------------------------------------

resource "aws_s3_bucket" "files" {
  bucket        = local.files_bucket
  force_destroy = !var.deletion_protection
}

resource "aws_s3_bucket_public_access_block" "files" {
  bucket                  = aws_s3_bucket.files.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "files" {
  bucket = aws_s3_bucket.files.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "files" {
  bucket = aws_s3_bucket.files.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# A file the application deletes (an attachment removed, a business erased on request) must really
# go. With versioning on, a delete only hides the object; so earlier versions are removed after a
# short time, long enough to undo a mistake and no longer.
resource "aws_s3_bucket_versioning" "files" {
  bucket = aws_s3_bucket.files.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "files" {
  bucket     = aws_s3_bucket.files.id
  depends_on = [aws_s3_bucket_versioning.files]

  rule {
    id     = "remove-deleted-files-for-good"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    expiration {
      expired_object_delete_marker = true
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

# --- encrypted database backups ---------------------------------------------------------------

resource "aws_s3_bucket" "backups" {
  bucket        = local.backups_bucket
  force_destroy = !var.deletion_protection
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Not versioned on purpose: the retention window must be exactly what the lifecycle rule says.
resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "backup-retention"
    status = "Enabled"
    filter {}

    expiration {
      days = var.backup_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

# --- both buckets: encrypted connections only --------------------------------------------------

data "aws_iam_policy_document" "tls_only" {
  for_each = { files = aws_s3_bucket.files.arn, backups = aws_s3_bucket.backups.arn }

  statement {
    sid       = "DenyUnencryptedTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value, "${each.value}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "files" {
  bucket     = aws_s3_bucket.files.id
  policy     = data.aws_iam_policy_document.tls_only["files"].json
  depends_on = [aws_s3_bucket_public_access_block.files]
}

resource "aws_s3_bucket_policy" "backups" {
  bucket     = aws_s3_bucket.backups.id
  policy     = data.aws_iam_policy_document.tls_only["backups"].json
  depends_on = [aws_s3_bucket_public_access_block.backups]
}
