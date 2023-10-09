#!/bin/bash
#
# This script generates the certificates and keys required for development and testing.
# It should be run from the root directory of the project. It takes several arguments:
# 1. The C for the root certificate
# 2. The ST for the root certificate
# 3. The L for the root certificate
# 4. The O for the root certificate
# 5. The OU for the root certificate
# 5. The CN for the root certificate

# Ensure that the script is run with the required arguments
if [ "$#" -ne 6 ]; then
    echo "Usage: ./generate.sh <root C> <root ST> <root L> <root O> <root OU> <root CN>"
    exit 1
fi

# Prompt for variable confirmation

echo "Generating certificates with the following variables:"
echo "Country: $1"
echo "State: $2"
echo "Local: $3"
echo "Organization: $4"
echo "Organizational Unit: $5"
echo "Common Name: $6"
echo "Is this correct? (y/n)"
read -r confirmation

# Exit if the variables are incorrect

if [ "$confirmation" != "y" ]; then
    echo "Exiting..."
    exit 1
fi

# Format the variables for each certificate

root_subject="/C=$1/ST=$2/L=$3/O=$4/OU=$5/CN=$6"
api_subject="/C=$1/ST=$2/L=$3/O=$4/OU=$5/CN=api.$6"
www_subject="/C=$1/ST=$2/L=$3/O=$4/OU=$5/CN=www.$6"
auth_subject="/C=$1/ST=$2/L=$3/O=$4/OU=$5/CN=auth.$6"
db_subject="/C=$1/ST=$2/L=$3/O=$4/OU=$5/CN=db.$6"

# Step 1: Generate the root certificate and key
openssl req -newkey rsa:4096 -x509 -sha256 -days 365 -nodes -out root.crt \
 --keyout root.key -subj "$root_subject"

# Step 2: Generate the certificate signing requests for the API, WWW, and Auth certificates
openssl req -newkey rsa:4096 -nodes -out api.csr -keyout api.key -subj "$api_subject" \
  -addext "subjectAltName = DNS:$6"
openssl req -newkey rsa:4096 -nodes -out www.csr -keyout www.key -subj "$www_subject" \
  -addext "subjectAltName = DNS:$6"
openssl req -newkey rsa:4096 -nodes -out auth.csr -keyout auth.key -subj "$auth_subject" \
  -addext "subjectAltName = DNS:$6"
openssl req -newkey rsa:4096 -nodes -out db.csr -keyout db.key -subj "$db_subject" \
  -addext "subjectAltName = DNS:$6"

# Step 3: Use the root key to sign the certificate signing requests
openssl x509 -req -in api.csr -CA root.crt -CAkey root.key -CAcreateserial -out api.crt -days 365 -sha256 \
  -extensions v3_req -extfile <(printf "[v3_req]\nsubjectAltName = DNS:$6")
openssl x509 -req -in www.csr -CA root.crt -CAkey root.key -CAcreateserial -out www.crt -days 365 -sha256 \
  -extensions v3_req -extfile <(printf "[v3_req]\nsubjectAltName = DNS:$6")
openssl x509 -req -in auth.csr -CA root.crt -CAkey root.key -CAcreateserial -out auth.crt -days 365 -sha256 \
  -extensions v3_req -extfile <(printf "[v3_req]\nsubjectAltName = DNS:$6")
openssl x509 -req -in db.csr -CA root.crt -CAkey root.key -CAcreateserial -out db.crt -days 365 -sha256 \
  -extensions v3_req -extfile <(printf "[v3_req]\nsubjectAltName = DNS:$6")

# Step 4: Move the certificates to the 'certs' directory, creating it if necessary

mkdir -p certs
mv root.crt certs/
mv ./*.srl certs/
mv ./*.crt certs/
mv ./*.key certs/

# Step 5: Remove the certificate signing requests and the root key

rm ./*.csr
rm ./certs/root.key

# Step 6: Create the `certs` development directories for the API and WWW (NPM) servers

mkdir -p api/certs
mkdir -p www/app/certs
cp ./certs/api.crt api/certs/
cp ./certs/api.key api/certs/
cp ./certs/root.crt api/certs/
cp ./certs/www.crt www/app/certs/
cp ./certs/www.key www/app/certs/
cp ./certs/root.crt www/app/certs/

# Prompt to display the certificates

echo "Certificates generated. Would you like to display them? (y/n)"
read -r display

if [ "$display" == "y" ]; then
    echo "----- Root certificate -----"
    openssl x509 -in certs/root.crt -text -noout
    echo "----- API certificate -----"
    openssl x509 -in certs/api.crt -text -noout
    echo "----- WWW certificate -----"
    openssl x509 -in certs/www.crt -text -noout
    echo "----- Auth certificate -----"
    openssl x509 -in certs/auth.crt -text -noout
    echo "----- DB certificate -----"
    openssl x509 -in certs/db.crt -text -noout
fi