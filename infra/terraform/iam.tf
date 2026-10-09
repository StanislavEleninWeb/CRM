# Two identities, each able to do one thing. Their access keys are NOT created here: a key made by
# Terraform would sit in the state file. Create them once by hand (see README) and put them
# straight into the server's settings files.

resource "aws_iam_user" "app" {
  name = "${var.name}-app"
  path = "/service/"
}

data "aws_iam_policy_document" "app" {
  # Objects in the files bucket and nothing else: no listing, no bucket settings, no other bucket.
  statement {
    sid       = "FilesObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.files.arn}/*"]
  }
}

resource "aws_iam_user_policy" "app" {
  name   = "files-objects"
  user   = aws_iam_user.app.name
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_user" "backup" {
  name = "${var.name}-backup"
  path = "/service/"
}

data "aws_iam_policy_document" "backup" {
  # May add backups and read them back for a restore. Cannot delete or overwrite retention:
  # a compromised server cannot destroy its own backups.
  statement {
    sid       = "WriteAndReadBackups"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.backups.arn}/*"]
  }
  statement {
    sid       = "ListBackups"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.backups.arn]
  }
}

resource "aws_iam_user_policy" "backup" {
  name   = "backups-write-read"
  user   = aws_iam_user.backup.name
  policy = data.aws_iam_policy_document.backup.json
}
