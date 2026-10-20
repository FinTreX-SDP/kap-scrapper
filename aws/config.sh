# Settings shared by the AWS scripts; sourced by them, not run.
export MSYS_NO_PATHCONV=1  # else Git Bash turns arguments such as /ecs/kapscrapper into Windows paths
export AWS_DEFAULT_REGION=eu-central-1  # Frankfurt, near KAP

AWS_EXE=$(command -v aws || echo "/c/Program Files/Amazon/AWSCLIV2/aws.exe")
aws() { "$AWS_EXE" "$@" | tr -d '\r'; }  # the Windows CLI ends lines with \r

PROJECT=kapscrapper
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=$PROJECT-$ACCOUNT-$AWS_DEFAULT_REGION
REGISTRY=$ACCOUNT.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)

sg() {  # id of the security group named $1 in the default VPC; created with description $2 if missing
    local id
    id=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values="$VPC" Name=group-name,Values="$1" \
        --query 'SecurityGroups[0].GroupId' --output text)
    if [ "$id" = None ]; then
        id=$(aws ec2 create-security-group --vpc-id "$VPC" --group-name "$1" --description "$2" --query GroupId --output text)
    fi
    echo "$id"
}
