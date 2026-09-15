#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lab


BUCKET = "yuntech-115-1-w2-private-spencerku"
OBJECT_KEY = "lab/w2/hello.txt"
SOURCE = ROOT / "practice/w2/helle.txt"
URL_SECONDS = 60


def validate_bucket(bucket):
    if bucket != BUCKET:
        raise lab.LabError(f"Refusing unscoped bucket: {bucket}")


def create_bucket(region, bucket):
    args = ["s3api", "create-bucket", "--bucket", bucket]
    if region != "us-east-1":
        args.extend(["--create-bucket-configuration", f"LocationConstraint={region}"])
    lab.run_aws(args, region)


def configure_bucket(region, bucket):
    lab.run_aws(
        [
            "s3api",
            "put-bucket-ownership-controls",
            "--bucket",
            bucket,
            "--ownership-controls",
            "Rules=[{ObjectOwnership=BucketOwnerEnforced}]",
        ],
        region,
    )
    lab.run_aws(
        [
            "s3api",
            "put-public-access-block",
            "--bucket",
            bucket,
            "--public-access-block-configuration",
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
        ],
        region,
    )
    lab.run_aws(
        [
            "s3api",
            "put-bucket-encryption",
            "--bucket",
            bucket,
            "--server-side-encryption-configuration",
            '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}',
        ],
        region,
    )


def verify_download(region, bucket, source, object_key):
    source_bytes = source.read_bytes()
    with tempfile.TemporaryDirectory(prefix="w2-s3-") as directory:
        downloaded = Path(directory) / "downloaded.txt"
        lab.run_aws(
            ["s3api", "get-object", "--bucket", bucket, "--key", object_key, str(downloaded)],
            region,
        )
        downloaded_bytes = downloaded.read_bytes()
    if source_bytes != downloaded_bytes:
        raise lab.LabError("Downloaded bytes differ from the local source")
    return len(source_bytes), hashlib.sha256(source_bytes).hexdigest()


def verify_configuration(region, bucket):
    public_block = lab.run_aws(["s3api", "get-public-access-block", "--bucket", bucket], region)
    configuration = public_block.get("PublicAccessBlockConfiguration", {})
    expected = {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
    if configuration != expected:
        raise lab.LabError("Public access block configuration is not fully enabled")

    encryption = lab.run_aws(["s3api", "get-bucket-encryption", "--bucket", bucket], region)
    rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
    algorithm = rules[0].get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") if rules else None
    if algorithm != "AES256":
        raise lab.LabError("Bucket default encryption is not SSE-S3/AES256")

    head = lab.run_aws(["s3api", "head-object", "--bucket", bucket, "--key", OBJECT_KEY], region)
    if head.get("ServerSideEncryption") != "AES256":
        raise lab.LabError("Object encryption is not SSE-S3/AES256")
    return {"public_access_block": expected, "encryption": algorithm, "object_encryption": head["ServerSideEncryption"]}


def upload(args):
    ctx = lab.verify()
    region = ctx["region"]
    bucket = args.bucket
    source = Path(args.source)
    validate_bucket(bucket)
    if not source.is_file():
        raise lab.LabError(f"Source file not found: {source}")

    lab.approve(
        f"Create private S3 bucket {bucket} in {region}, upload {OBJECT_KEY}, estimated cost is S3 storage/requests only.",
        "CREATE-W2",
    )
    create_bucket(region, bucket)
    configure_bucket(region, bucket)
    lab.run_aws(
        [
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            OBJECT_KEY,
            "--body",
            str(source),
            "--server-side-encryption",
            "AES256",
        ],
        region,
    )
    size, digest = verify_download(region, bucket, source, OBJECT_KEY)
    settings = verify_configuration(region, bucket)
    lab.run_aws(
        ["s3", "presign", f"s3://{bucket}/{OBJECT_KEY}", "--expires-in", str(URL_SECONDS)],
        region,
        raw=True,
    )
    print(json.dumps({"bucket": bucket, "region": region, "object": OBJECT_KEY, "bytes": size, "sha256": digest, "presign_seconds": URL_SECONDS, **settings}))
    print("Presigned URL generated for validation and withheld from output by repository policy.")


def cleanup(args):
    ctx = lab.verify()
    region = ctx["region"]
    bucket = args.bucket
    validate_bucket(bucket)
    objects = lab.run_aws(["s3api", "list-objects-v2", "--bucket", bucket], region)
    keys = [item["Key"] for item in objects.get("Contents", [])]
    if keys != [OBJECT_KEY]:
        raise lab.LabError("Refusing cleanup: bucket does not contain exactly the scoped object")
    lab.approve(
        f"Delete object {OBJECT_KEY} and bucket {bucket}; no other resources will be touched.",
        "DELETE-W2",
    )
    lab.run_aws(["s3api", "delete-object", "--bucket", bucket, "--key", OBJECT_KEY], region)
    lab.run_aws(["s3api", "delete-bucket", "--bucket", bucket], region)
    print(json.dumps({"deleted_object": OBJECT_KEY, "deleted_bucket": bucket, "region": region}))


def copy_url(args):
    ctx = lab.verify()
    validate_bucket(args.bucket)
    url = lab.run_aws(
        ["s3", "presign", f"s3://{args.bucket}/{OBJECT_KEY}", "--expires-in", str(URL_SECONDS)],
        ctx["region"],
        raw=True,
    )
    subprocess.run(["pbcopy"], input=url, text=True, check=True)
    print(json.dumps({"bucket": args.bucket, "region": ctx["region"], "object": OBJECT_KEY, "expires_in": URL_SECONDS}))
    print("Presigned URL copied to the local macOS clipboard; it is not printed.")


def main():
    parser = argparse.ArgumentParser(description="W2 scoped private S3 workflow")
    parser.add_argument("command", choices=["upload", "copy-url", "cleanup"])
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--source", default=str(SOURCE))
    args = parser.parse_args()
    try:
        {"upload": upload, "copy-url": copy_url, "cleanup": cleanup}[args.command](args)
    except (lab.LabError, OSError, ValueError, KeyError) as exc:
        print("STOP: " + str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())