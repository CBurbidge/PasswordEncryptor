import base64
import uuid
import json
import logging
import os

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

logger = logging.getLogger()
logger.setLevel(logging.INFO)

random_passwords_to_encrypt = 12
password_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_!"
password_length = 15

# This function takes in the values from the stack which are sensitive and encrypts them with the key specified.abs
# It then returns them to be used in the CustomResource in the cloudformation stack

def send_response(httplib, request, response, status=None, reason=None):
    """ Send our response to the pre-signed URL supplied by CloudFormation
    If no ResponseURL is found in the request, there is no place to send a
    response. This may be the case if the supplied event was for testing.
    """

    if status is not None:
        response['Status'] = status

    if reason is not None:
        response['Reason'] = reason

    if 'ResponseURL' in request and request['ResponseURL']:
        url = urlparse(request['ResponseURL'])
        body = json.dumps(response)
        https = httplib.HTTPSConnection(url.hostname)
        https.request('PUT', url.path+'?'+url.query, body)

    return response

def failed_response(httplib, failed_reason, event_arg, response_arg):
    return send_response(httplib,
        event_arg, response_arg, status='FAILED',
        reason=failed_reason
    )

def key_exists(s3_client, bucket, key):
    results = s3_client.list_objects(Bucket=bucket, Prefix=key)
    return 'Contents' in results

def encrypt(kms_client, key_id, plaintext):
    encrypted = kms_client.encrypt(KeyId=key_id, Plaintext=plaintext)
    val = base64.b64encode(encrypted['CiphertextBlob'])
    return val.decode("utf-8")

def get_random_password():
    password = ""
    random_bytes = os.urandom(password_length)
    for n in range(0, password_length):
        random_byte = random_bytes[n]
        mod64 = int(random_byte) % 64
        password += password_chars[mod64]
    return password

def get_password_name(n):
    return "Password" + str(n) + "Encrypted"

def handler_impl(event, context, boto, httplib):
    logger.info("ResponseUrl is " + event['ResponseURL'])
    response = {
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Status': 'SUCCESS'
    }

    # PhysicalResourceId is meaningless here, but CloudFormation requires it
    if 'PhysicalResourceId' in event:
        response['PhysicalResourceId'] = event['PhysicalResourceId']
    else:
        response['PhysicalResourceId'] = str(uuid.uuid4())

    # There is nothing to do for a delete request
    if event['RequestType'] == 'Delete':
        return send_response(httplib, event, response)

    kms_client = boto.client('kms')

    # Encrypt the values using AWS KMS and return the response
    try:
        res_props = event['ResourceProperties']
        if 'KeyId' not in res_props or not res_props['KeyId']:
            logger.info("KeyId validation failed")
            return failed_response(httplib, 'KeyId not present', event, response)

        key_id = res_props['KeyId']
        data = dict()
        encrypt_placeholder = "Encrypt_"
        to_encrypt_keys =  list(filter(lambda key: key.startswith(encrypt_placeholder), res_props.keys()))
        for to_encrypt_key in to_encrypt_keys:
            name = to_encrypt_key[len(encrypt_placeholder):] + "Encrypted"
            plaintext = res_props[to_encrypt_key]
            data[name] = encrypt(kms_client, key_id, plaintext)

        if 'BucketName' in res_props:
            logger.info("BucketName specified, try to write many random passwords")
            bucket = res_props["BucketName"]
            key = event["StackId"] + "_" + event["LogicalResourceId"]
            s3_client = boto.client("s3")
            if key_exists(s3_client, bucket, key):
                object_response = s3_client.get_object(Bucket=bucket, Key=key)
                file_contents = object_response['Body'].read().decode('utf-8')
                json_contents = json.loads(file_contents)
            else:
                passwords = {}
                for n in range(0, random_passwords_to_encrypt + 1):
                    password_name = get_password_name(n)
                    random_password = get_random_password()
                    password_encrypted = encrypt(kms_client, key_id, random_password)
                    passwords[password_name] = password_encrypted
                passwords_as_json = json.dumps(passwords)
                s3_client.put_object(Bucket=bucket, Key=key, Body=passwords_as_json)
                json_contents = passwords
            for n in range(1, random_passwords_to_encrypt - len(to_encrypt_keys) + 1):
                password_name = get_password_name(n)
                if password_name in json_contents:
                    data[password_name] = json_contents[password_name]

        response['Data'] = data
        response['Reason'] = 'The value was successfully encrypted'

    except Exception as E:
        logger.error("Error - " + str(E))
        response['Status'] = 'FAILED'
        response['Reason'] = 'Encryption Failed - See CloudWatch logs for the Lamba function backing the custom resource for details'

    return send_response(httplib, event, response)