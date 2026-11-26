terraform {
  backend "s3" {
    key          = "replate/terraform.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}
