#!/usr/bin/env bash
# Make a key pair so the GitHub Action can sign in to Snowflake without a password.
# Run on your laptop:  bash trigger/keygen.sh
# Then: 1) run the printed ALTER USER in a Snowsight worksheet
#       2) paste the contents of guardian_rsa_key.p8 into the repo secret SNOWFLAKE_PRIVATE_KEY
#       3) delete both key files (never commit them)
set -euo pipefail
openssl genrsa 2048 2>/dev/null | openssl pkcs8 -topk8 -inform PEM -out guardian_rsa_key.p8 -nocrypt
openssl rsa -in guardian_rsa_key.p8 -pubout -out guardian_rsa_key.pub 2>/dev/null
PUB=$(grep -v -- '-----' guardian_rsa_key.pub | tr -d '\n')
echo
echo "1) Run this in Snowsight (replace <USER> with the output of SELECT CURRENT_USER();):"
echo
echo "   ALTER USER <USER> SET RSA_PUBLIC_KEY='${PUB}';"
echo
echo "2) Copy the private key into the GitHub secret SNOWFLAKE_PRIVATE_KEY:"
echo "   macOS:  pbcopy < guardian_rsa_key.p8      (or open the file and copy all of it)"
echo
echo "3) Then delete the files:  rm guardian_rsa_key.p8 guardian_rsa_key.pub"
