# Renew-Cloudfront-China-Certificate
Automatic renew AWS China Cloudfront SSL certificate from Let's Encrypt 

USAGE:
    renew_cloudfront_cert_china.py -d <domain_name> -c <cloudfront_id> [options]

DESCRIPTION:
    Automates the process of obtaining/renewing ACME (Let's Encrypt) SSL certificates
    using acme.sh with DNS-01 validation (via DNSPod) and updating an AWS China
    CloudFront distribution to use the new certificate.

    Handles wildcard domains and the requirement to upload IAM Server Certificates
    to the cn-north-1 region for CloudFront compatibility.

REQUIRED ARGUMENTS:
    -d, --domain DOMAIN
        The domain name for the certificate. Use '*' for wildcards
        (e.g., 'example.com', '*.example.com').
    -c, --cloudfront-id CLOUDFRONT_ID
        The ID of the CloudFront distribution to update (e.g., 'E123EXAMPLEID').

OPTIONS:
    -r, --region REGION
        Primary AWS China Region (for CloudFront client context and tagging).
        Default: cn-north-1
    -h, --help
        Show this help message and exit.

ACME OPTIONS (used if not providing local cert paths):
    --acme-sh-path PATH
        Path to the acme.sh executable.
        Default: acme.sh
    --acme-home PATH
        Path to the acme.sh base configuration directory (parent of 'certs').
        Default: /Applications/ServBay/etc/acme
    --key-length LENGTH
        Key length/type for ACME certificate (e.g., 'ec-256', '4096').
        Default: ec-256

DEBUGGING OPTIONS (use existing local certificates instead of running acme.sh):
    --cert-path PATH
        Full path to the existing server certificate file (domain.cer).
    --key-path PATH
        Full path to the existing private key file (domain.key).
    --chain-path PATH
        Full path to the existing intermediate chain file (ca.cer).
        (Requires all three --cert-path, --key-path, --chain-path to be specified)

CONTROL FLAGS:
    --skip-cleanup
        Do not delete older IAM certificates (matching the script's prefix)
        from the cn-north-1 region after a successful update.
    --no-aws
        Only run the acme.sh issuance/renewal step. Do not perform any AWS
        operations (IAM upload, CloudFront update, cleanup). Cannot be used
        with local certificate path arguments.

ENVIRONMENT VARIABLES:
    The script relies on the following environment variables:
    - DP_Id: Your DNSPod API ID (required for acme.sh dns_dp plugin).
    - DP_Key: Your DNSPod API Token (required for acme.sh dns_dp plugin).
    - AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, (optionally AWS_SESSION_TOKEN):
        AWS credentials with permissions for IAM in cn-north-1 (upload/list/delete certs)
        and CloudFront in the specified China region (get/update distribution).
    - AWS_REGION: Can be used by Boto3 to determine the primary region if -r is not specified.

EXAMPLES:
    # Standard usage (generate cert and update CF):
    export DP_Id="YOUR_ID" DP_Key="YOUR_TOKEN"
    export AWS_ACCESS_KEY_ID="YOUR_AWS_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="YOUR_AWS_ACCESS_KEY"
    ./renew_cloudfront_cert_china.py -d www.bra.live -c E123EXAMPLEID -r cn-north-1

    # Standard usage for a wildcard domain:
    export DP_Id="YOUR_ID" DP_Key="YOUR_TOKEN"
    export AWS_ACCESS_KEY_ID="YOUR_AWS_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="YOUR_AWS_ACCESS_KEY"
    ./renew_cloudfront_cert_china.py -d '*.bra.live' -c E456WILDCARDID -r cn-northwest-1

    # Use existing local certificate files for debugging AWS steps:
    ./renew_cloudfront_cert_china.py -d www.bra.live -c E123EXAMPLEID \
        --cert-path /path/to/certs/www.bra.live/www.bra.live.cer \
        --key-path /path/to/certs/www.bra.live/www.bra.live.key \
        --chain-path /path/to/certs/www.bra.live/ca.cer

    # Generate/renew cert only, skip AWS interaction:
    export DP_Id="YOUR_ID" DP_Key="YOUR_TOKEN"
    ./renew_cloudfront_cert_china.py -d test.bra.live -c DUMMY_ID --no-aws
