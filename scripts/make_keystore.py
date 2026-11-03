#!/usr/bin/env python3
"""Make an Android signing keystore (PKCS12) without Java, for example in a cloud sandbox that has no keytool.
Usage: python3 scripts/make_keystore.py <out file> <alias> <password> ["Common Name"]
Prints the base64 of the file afterwards, which goes into the repo secret SIGNING_KEYSTORE_B64 (with SIGNING_ALIAS
and SIGNING_PASS) for the APK workflow. Keep the file or the secret: an app signed with another key can't update
in place. Needs: pip install cryptography"""
import sys, base64, datetime
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
out, alias, password = sys.argv[1], sys.argv[2], sys.argv[3]
cn = sys.argv[4] if len(sys.argv) > 4 else "Trip app"
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
now = datetime.datetime.now(datetime.timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=10000)).sign(key, hashes.SHA256()))
# PBES1 (3DES/SHA1) is what every Java version reads; the keystore only guards a hobby app's key.
enc = (serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(50000)
       .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC).hmac_hash(hashes.SHA1()).build(password.encode()))
data = pkcs12.serialize_key_and_certificates(alias.encode(), key, cert, None, enc)
open(out, "wb").write(data)
print("wrote", out, "alias", alias)
print("SIGNING_KEYSTORE_B64 (one line):")
print(base64.b64encode(data).decode())
