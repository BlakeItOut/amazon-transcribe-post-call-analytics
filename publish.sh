#!/bin/bash

##############################################################################################
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
##############################################################################################

##############################################################################################
# Create new Cfn artifacts bucket if not already existing
# Modify templates to reference new bucket names and prefixes
# create lambda zipfiles with timestamps to ensure redeployment on stack update
# Upload templates to S3 bucket
#
# To deploy to non-default region, set AWS_DEFAULT_REGION to supported region
# See: https://aws.amazon.com/about-aws/global-infrastructure/regional-product-services/ - E.g.
# export AWS_DEFAULT_REGION=eu-west-1
##############################################################################################

USAGE="$0 cfn_bucket cfn_prefix"

BUCKET=$1
[ -z "$BUCKET" ] && echo "Cfn bucket name is required parameter. Usage $USAGE" && exit 1

PREFIX=$2
[ -z "$PREFIX" ] && echo "Prefix is required parameter. Usage $USAGE" && exit 1

# Add trailing slash to prefix if needed
[[ "${PREFIX}" != */ ]] && PREFIX="${PREFIX}/"

# get bucket region for owned accounts
region=$(aws s3api get-bucket-location --bucket $BUCKET --query "LocationConstraint" --output text) || region="us-east-1"
[ -z "$region" -o "$region" == "None" ] && region=us-east-1;
echo "Bucket in region: $region"

# Create bucket if it doesn't already exist
aws s3api list-buckets --query 'Buckets[].Name' | grep "\"$BUCKET\"" > /dev/null 2>&1
if [ $? -ne 0 ]; then
  echo "Creating s3 bucket: $BUCKET"
  aws s3 mb s3://${BUCKET} || exit 1
  aws s3api put-bucket-versioning --bucket ${BUCKET} --versioning-configuration Status=Enabled || exit 1
else
  echo "Using existing bucket: $BUCKET"
fi

echo "Getting package dependencies"
pushd pca-server/src/trigger
npm install
popd
# no need to install python packages.. only boto3 and it's included in Lambda runtime
pushd pca-ui/src/lambda
npm install
popd


echo "Packaging Cfn artifacts"
aws cloudformation package --template-file main.template --output-template-file packaged.template --s3-bucket ${BUCKET} --s3-prefix ${PREFIX} || exit 1
aws s3 cp packaged.template s3://${BUCKET}/${PREFIX}pca-main.yaml --acl public-read || exit 1

echo "Validating Cfn artifacts"
template="https://s3.${region}.amazonaws.com/${BUCKET}/${PREFIX}pca-main.yaml"
aws cloudformation validate-template --template-url $template > /dev/null || exit 1


echo "Outputs"
echo PCA - Template URL: $template
echo PCA - CF Launch URL: https://${region}.console.aws.amazon.com/cloudformation/home?region=${region}#/stacks/create/review?templateURL=${template}\&stackName=PostCallAnalytics
echo PCA - CLI Deploy: aws cloudformation deploy --template-file `pwd`/packaged.template --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND --stack-name PostCallAnalytics --parameter-overrides AdminEmail=johndoe@example.com

echo Done
exit 0

