# AWS resources for the CRM

Terraform for everything the CRM uses in AWS (`eu-central-1`): two S3 buckets, two IAM users limited to them, and the Cognito user pool people sign in with.

**State: written and validated (`terraform validate`, provider 6.68). Never planned or applied against an AWS account.** Read the plan before the first apply.

| Resource | Purpose | Notable settings |
|---|---|---|
| Files bucket | Uploaded prospect lists, attachments | Private, encrypted, TLS only. Versioned, with earlier versions removed after 7 days, so a deleted file is really gone a week later |
| Backups bucket | Encrypted database dumps | Private, encrypted, TLS only. Objects expire after 30 days (`backup_retention_days`) |
| IAM user `…-app` | The application | Get, put and delete objects in the files bucket. Nothing else |
| IAM user `…-backup` | The backup job | Put and get in the backups bucket, and list it. Cannot delete |
| Cognito user pool | Sign-in | Email as user name, no self-registration, MFA required (authenticator app), deletion protection |
| Cognito app client | The application | Has a secret; authorization-code flow; scopes `openid email profile`; callback `https://crm.seweb.co/api/v1/auth/callback` |

## Before the first run: where the state lives

The state file contains the Cognito client secret. It must not be in the repository (it is git-ignored) and should not live on a laptop. Create one private, versioned, encrypted bucket for Terraform state by hand, once:

```bash
aws s3api create-bucket --bucket <your-state-bucket> --region eu-central-1 --create-bucket-configuration LocationConstraint=eu-central-1
```

```bash
aws s3api put-public-access-block --bucket <your-state-bucket> --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

```bash
aws s3api put-bucket-versioning --bucket <your-state-bucket> --versioning-configuration Status=Enabled
```

Then copy `backend.hcl.example` to `backend.hcl` and put the bucket name in it.

## Apply

```bash
terraform init -backend-config=backend.hcl
```

```bash
terraform plan -out=crm.tfplan
```

Read the plan: it should create about 20 resources and change or destroy none. Then:

```bash
terraform apply crm.tfplan
```

If the Cognito domain prefix `seweb-crm` is taken in the region, set another: `-var cognito_domain_prefix=…`.

## After apply: the values the server needs

```bash
terraform output
```

```bash
terraform output -raw oidc_client_secret
```

| Output | Goes to |
|---|---|
| `oidc_issuer`, `oidc_client_id`, `oidc_client_secret` | `/etc/seweb-crm/env` |
| `files_bucket`, `s3_region` | `/etc/seweb-crm/env` (`S3_BUCKET`, `S3_REGION`; leave `S3_ENDPOINT_URL` empty) |
| `backups_uri` | `/etc/seweb-crm/deploy.conf` (`BACKUP_S3_URI`) |

**Access keys are deliberately not created by Terraform**, so they never enter the state. Create one for each user and type it straight into the server's file:

```bash
aws iam create-access-key --user-name seweb-crm-app
```

```bash
aws iam create-access-key --user-name seweb-crm-backup
```

The first goes into `/etc/seweb-crm/env` (`S3_ACCESS_KEY`, `S3_SECRET_KEY`), the second into `/etc/seweb-crm/backup-aws.env`.

## People

Users are created by an administrator; there is no self-registration.

```bash
aws cognito-idp admin-create-user --user-pool-id "$(terraform output -raw user_pool_id)" --username owner@seweb.co --user-attributes Name=email,Value=owner@seweb.co Name=email_verified,Value=true
```

The person receives a temporary password by email, sets their own, and enrols an authenticator app at first sign-in. `email_verified` must be true: the application refuses a sign-in without it, and an invitation can only be accepted by the address it was sent to.

## Things to know

- **Changing `backup_retention_days`** changes how long deleted data stays recoverable. Update `docs/data-retention.md` in the same change.
- **`deletion_protection = true`** makes Terraform refuse to delete the user pool and non-empty buckets. Leave it on.
- **The user pool's attribute schema cannot be changed after creation.** Terraform is told to ignore differences there.
- **Not managed here:** the Terraform state bucket itself, the access keys, the users, DNS and Cloudflare, and anything on the server.
- **Cost:** two small buckets and a user pool with a handful of users fall within or near AWS's free allowances; check the plan against current AWS pricing rather than taking that from here.
