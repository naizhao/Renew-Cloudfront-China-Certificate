#!/usr/bin/env python3
import os
import sys
import subprocess
import logging
import argparse
import time
import datetime
from datetime import timezone
import boto3
from botocore.exceptions import ClientError
import json

DEFAULT_AWS_REGION = 'cn-north-1'
DEFAULT_ACME_HOME = os.path.expanduser("~/.acme.sh")
DEFAULT_KEY_LENGTH = "ec-256"
CERT_NAME_PREFIX = "acme-cloudfront-china-"

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

def run_command(command):
    """Executes a shell command and returns its output."""
    logging.info(f"Running command: {' '.join(command)}")
    try:
        result = subprocess.run(command,
                                capture_output=True,
                                text=True,
                                check=True,
                                env=os.environ)
        logging.info(f"Command stdout:\n{result.stdout}")
        if result.stderr:
            logging.warning(f"Command stderr:\n{result.stderr}")
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed with exit code {e.returncode}:")
        logging.error(f"Stdout: {e.stdout}")
        logging.error(f"Stderr: {e.stderr}")
        raise
    except Exception as e:
        logging.error(f"Failed to run command: {e}")
        raise

def issue_or_renew_cert(domain, acme_sh_path, acme_home, key_length):
    """Uses acme.sh to issue or renew a certificate using DNS-01 with dnspod."""
    logging.info(f"Attempting to issue/renew certificate for domain: {domain}")

    if not os.environ.get('DP_Id') or not os.environ.get('DP_Key'):
        raise ValueError("Error: Environment variables DP_Id and DP_Key for DNSPod must be set.")

    command = [
        acme_sh_path,
        "--issue",
        "--dns", "dns_dp",
        "-d", domain,
        "--cert-home", acme_home,
        "--keylength", key_length,
        "--server", "letsencrypt",
        # "--force", # Uncomment to force renewal
    ]

    try:
        run_command(command)
        logging.info(f"acme.sh command completed successfully for {domain}.")

        # certs_base_dir = os.path.join(acme_home, "certs") # Uncomment if using ServBay's acme.sh
        certs_base_dir = acme_home # Use the default certs directory if not using ServBay's acme.sh

        domain_cert_dir_ecc = os.path.join(certs_base_dir, f"{domain}_ecc")
        domain_cert_dir_rsa = os.path.join(certs_base_dir, domain)

        if os.path.isdir(domain_cert_dir_ecc):
            domain_cert_dir = domain_cert_dir_ecc
            logging.info(f"Found ECC certificate directory: {domain_cert_dir}")
        elif os.path.isdir(domain_cert_dir_rsa):
            domain_cert_dir = domain_cert_dir_rsa
            logging.info(f"Found RSA certificate directory: {domain_cert_dir}")
        else:
            logging.error(f"Could not find certificate directory for {domain} under {certs_base_dir}")
            logging.error(f"Checked: {domain_cert_dir_ecc}")
            logging.error(f"Checked: {domain_cert_dir_rsa}")
            if os.path.exists(certs_base_dir):
                 logging.error(f"Contents of {certs_base_dir}: {os.listdir(certs_base_dir)}")
            else:
                 logging.error(f"{certs_base_dir} does not exist.")
            raise FileNotFoundError(f"Certificate directory not found for {domain}.")

        cert_path = os.path.join(domain_cert_dir, f"{domain}.cer")
        key_path = os.path.join(domain_cert_dir, f"{domain}.key")
        ca_path = os.path.join(domain_cert_dir, "ca.cer")

        required_files = {
            "certificate": cert_path,
            "private key": key_path,
            "CA chain": ca_path
        }
        missing_files = []
        for name, path in required_files.items():
            if not os.path.exists(path):
                missing_files.append(f"{name} ({path})")

        if missing_files:
             logging.error(f"Could not find generated certificate files in {certs_dir}")
             logging.error(f"Missing: {', '.join(missing_files)}")
             logging.error(f"Contents of {certs_dir}: {os.listdir(certs_dir) if os.path.exists(certs_dir) else 'Not Found'}")
             raise FileNotFoundError(f"Required certificate files not found for {domain}.")

        return cert_path, key_path, ca_path

    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to issue or renew certificate for {domain} via acme.sh.")
        logging.error(f"acme.sh exited with code: {e.returncode}")
        logging.error(f"acme.sh stdout:\n{e.stdout}")
        logging.error(f"acme.sh stderr:\n{e.stderr}")
        return None, None, None
    except FileNotFoundError as e:
        logging.error(f"File/Directory finding error after acme.sh execution: {e}")
        raise
    except Exception as e:
        logging.error(f"An unexpected error occurred during certificate issuance for {domain}: {e}", exc_info=True)
        return None, None, None

def upload_certificate_to_iam(iam_client, cert_body, private_key, chain_body, domain):
    """Uploads the certificate to the specified IAM region."""
    timestamp = datetime.datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    cert_name = f"{CERT_NAME_PREFIX}{domain}-{timestamp}"
    logging.info(f"Uploading certificate to IAM region {iam_client.meta.region_name} with name: {cert_name}")
    try:
        response = iam_client.upload_server_certificate(
            ServerCertificateName=cert_name,
            Path="/cloudfront/",
            CertificateBody=cert_body,
            PrivateKey=private_key,
            CertificateChain=chain_body,
            Tags=[{'Key': 'OriginRegion', 'Value': os.environ.get('AWS_REGION', 'cn-unknown')}]
        )
        metadata = response['ServerCertificateMetadata']
        cert_arn = metadata['Arn']
        cert_id = metadata['ServerCertificateId']
        logging.info(f"Successfully uploaded certificate to {iam_client.meta.region_name}: Name={cert_name}, ARN={cert_arn}, ID={cert_id}")
        return cert_arn, cert_id, cert_name
    except ClientError as e:
        if e.response['Error']['Code'] == 'EntityAlreadyExists':
             logging.error(f"Certificate name {cert_name} already exists in IAM region {iam_client.meta.region_name}.")
        elif e.response['Error']['Code'] == 'MalformedCertificate':
             logging.error(f"IAM in {iam_client.meta.region_name} rejected the certificate or chain format.")
        else:
             logging.error(f"Failed to upload certificate to IAM region {iam_client.meta.region_name}: {e}")
        raise

def update_cloudfront_distribution(cf_client, distribution_id, certificate_arn, certificate_id):
    """Updates the CloudFront distribution to use the new certificate ID."""
    logging.info(f"Updating CloudFront distribution {distribution_id} to use certificate ID {certificate_id} (ARN: {certificate_arn})")
    try:
        get_config_response = cf_client.get_distribution_config(Id=distribution_id)
        dist_config = get_config_response['DistributionConfig']
        etag = get_config_response['ETag']

        current_cert_id = dist_config.get('ViewerCertificate', {}).get('IAMCertificateId')
        if current_cert_id == certificate_id:
             logging.info(f"CloudFront distribution {distribution_id} is already using certificate ID {certificate_id}. No update needed.")
             return

        dist_config['ViewerCertificate'] = {
            'IAMCertificateId': certificate_id,
            'Certificate': certificate_id,
            'CertificateSource': 'iam',
            'SSLSupportMethod': 'sni-only',
            'MinimumProtocolVersion': 'TLSv1.2_2021',
        }
        dist_config.pop('CloudFrontDefaultCertificate', None)
        if 'ViewerCertificate' in dist_config and 'CloudFrontDefaultCertificate' in dist_config['ViewerCertificate']:
            del dist_config['ViewerCertificate']['CloudFrontDefaultCertificate']


        logging.info("Calling update_distribution with new Certificate ID...")
        update_response = cf_client.update_distribution(
            DistributionConfig=dist_config,
            Id=distribution_id,
            IfMatch=etag
        )
        logging.info(f"CloudFront distribution update initiated. Status: {update_response['Distribution']['Status']}")
        logging.info("Propagation can take 15-30 minutes.")

    except ClientError as e:
        logging.error(f"Failed to update CloudFront distribution {distribution_id}: {e}")
        if e.response['Error']['Code'] == 'InvalidArgument':
             logging.error("Received InvalidArgument. Check DistributionConfig parameters (SSL/TLS versions, etc).")
             logging.error(f"Config sent (relevant part): {json.dumps(dist_config.get('ViewerCertificate'))}")
        elif e.response['Error']['Code'] == 'InvalidViewerCertificate':
             logging.error(f"IAM Certificate ID {certificate_id} is invalid or not usable with CloudFront.")
        elif e.response['Error']['Code'] == 'PreconditionFailed':
             logging.error("ETag mismatch. Distribution config may have changed since fetch.")
        elif e.response['Error']['Code'] == 'NoSuchDistribution':
             logging.error(f"Distribution {distribution_id} not found.")
        raise

def cleanup_old_certificates(iam_client, cert_prefix, keep_arn):
    """Deletes old certificates uploaded by this script, keeping the latest one (identified by ARN)."""
    logging.info(f"Cleaning up old IAM certificates with prefix '{cert_prefix}', keeping ARN {keep_arn}")
    try:
        paginator = iam_client.get_paginator('list_server_certificates')
        certs_to_delete = []
        for page in paginator.paginate():
            for cert_meta in page.get('ServerCertificateMetadataList', []):
                cert_name = cert_meta['ServerCertificateName']
                cert_arn = cert_meta['Arn']
                if cert_name.startswith(cert_prefix) and cert_arn != keep_arn:
                    logging.info(f"Found potentially old certificate: {cert_name} (ARN: {cert_arn})")
                    certs_to_delete.append({'Name': cert_name, 'Arn': cert_arn})

        certs_to_delete.sort(key=lambda x: x['Name'])
        delete_count = 0
        for cert in certs_to_delete:
             logging.warning(f"Attempting to delete old certificate: {cert['Name']} (ARN: {cert['Arn']})")
             try:
                 iam_client.delete_server_certificate(ServerCertificateName=cert['Name'])
                 logging.info(f"Successfully deleted certificate: {cert['Name']}")
                 delete_count += 1
             except ClientError as e:
                 if e.response['Error']['Code'] == 'DeleteConflict':
                     logging.warning(f"Cannot delete certificate {cert['Name']}: Still in use.")
                 else:
                     logging.error(f"Failed to delete certificate {cert['Name']}: {e}")
        logging.info(f"Cleanup complete. Deleted {delete_count} old certificates.")
    except ClientError as e:
        logging.error(f"Failed during certificate cleanup: {e}")

def main():
    parser = argparse.ArgumentParser(description="Automate ACME SSL renewal and CloudFront update in AWS China.")
    parser.add_argument("-d", "--domain", required=True)
    parser.add_argument("-c", "--cloudfront-id", required=True)
    parser.add_argument("-r", "--region", default=DEFAULT_AWS_REGION)
    acme_group = parser.add_argument_group('ACME Options')
    acme_group.add_argument("--acme-sh-path", default="acme.sh")
    acme_group.add_argument("--acme-home", default=DEFAULT_ACME_HOME)
    acme_group.add_argument("--key-length", default=DEFAULT_KEY_LENGTH)
    debug_group = parser.add_argument_group('Debugging Options (use existing local certs)')
    debug_group.add_argument("--cert-path")
    debug_group.add_argument("--key-path")
    debug_group.add_argument("--chain-path")
    parser.add_argument("--skip-cleanup", action="store_true")
    parser.add_argument("--no-aws", action="store_true")
    args = parser.parse_args()

    using_local_certs = args.cert_path or args.key_path or args.chain_path
    if using_local_certs and not (args.cert_path and args.key_path and args.chain_path):
        parser.error("--cert-path, --key-path, and --chain-path must all be provided together.")
    if args.no_aws and using_local_certs:
        parser.error("--no-aws cannot be used with local cert paths.")

    logging.info("--- Starting Certificate Process ---")
    logging.info(f"Domain: {args.domain}, CF ID: {args.cloudfront_id}, Region: {args.region}")

    cert_file_path, key_file_path, chain_file_path = None, None, None
    new_cert_arn, new_cert_id, new_cert_name = None, None, None

    try:
        if using_local_certs:
            logging.info("Using local certificate files provided via arguments.")
            cert_file_path, key_file_path, chain_file_path = args.cert_path, args.key_path, args.chain_path
            for p in [cert_file_path, key_file_path, chain_file_path]:
                if not os.path.exists(p): raise FileNotFoundError(f"File not found: {p}")
            logging.info(f"Paths: Cert={cert_file_path}, Key={key_file_path}, Chain={chain_file_path}")
        elif not args.no_aws:
            logging.info(f"Using acme.sh: Path={args.acme_sh_path}, Home={args.acme_home}, KeyLen={args.key_length}")
            cert_file_path, key_file_path, chain_file_path = issue_or_renew_cert(
                args.domain, args.acme_sh_path, args.acme_home, args.key_length)
            logging.info(f"acme.sh finished. Paths: Cert={cert_file_path}, Key={key_file_path}, Chain={chain_file_path}")

        if args.no_aws:
            logging.info("--no-aws specified, skipping AWS operations.")
            logging.info("--- Process Finished (AWS Skipped) ---")
            sys.exit(0)

        if not cert_file_path:
             logging.warning("No certificate paths available and --no-aws specified earlier. Exiting.")
             sys.exit(0)

        logging.info("Reading certificate files for AWS upload...")
        with open(cert_file_path, 'r') as f: cert_body = f.read()
        with open(key_file_path, 'r') as f: private_key_body = f.read()
        with open(chain_file_path, 'r') as f: cert_chain_body = f.read()

        logging.info(f"Initializing Boto3 clients for region: {args.region}")
        session = boto3.Session(region_name=args.region)
        iam_client = session.client('iam')
        cf_client = session.client('cloudfront')

        clean_domain = args.domain.replace('*.', '_.')
        new_cert_arn, new_cert_id, new_cert_name = upload_certificate_to_iam(
            iam_client, cert_body, private_key_body, cert_chain_body, clean_domain
        )

        update_cloudfront_distribution(
            cf_client, args.cloudfront_id, new_cert_arn, new_cert_id
        )

        if not args.skip_cleanup and new_cert_arn:
            logging.info("Waiting before cleanup...")
            time.sleep(30)
            cert_prefix_for_cleanup = CERT_NAME_PREFIX + clean_domain + "-"
            cleanup_old_certificates(iam_client, cert_prefix_for_cleanup, new_cert_arn)
        else:
            logging.info("Skipping cleanup.")

        logging.info("--- AWS Operations Completed Successfully ---")

    except Exception as e:
        logging.error(f"An error occurred: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
