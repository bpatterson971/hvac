import binascii
import sys
from base64 import b64decode
from unittest import TestCase
from uuid import UUID

from hvac import Client, exceptions
from hvac.tests import util


def create_client(**kwargs):
    return Client(url='https://localhost:8200',
                  cert=('test/client-cert.pem', 'test/client-key.pem'),
                  verify='test/server-cert.pem',
                  **kwargs)


class IntegrationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = util.ServerManager(config_path='test/vault-tls.hcl', client=create_client())
        cls.manager.start()
        cls.manager.initialize()
        cls.manager.unseal()

    @classmethod
    def tearDownClass(cls):
        cls.manager.stop()

    def root_token(self):
        cls = type(self)
        return cls.manager.root_token

    def setUp(self):
        self.client = create_client(token=self.root_token())

    def test_unseal_multi(self):
        cls = type(self)

        self.client.seal()

        keys = cls.manager.keys

        result = self.client.unseal_multi(keys[0:2])

        assert result['sealed']
        assert result['progress'] == 2

        result = self.client.unseal_reset()
        assert result['progress'] == 0
        result = self.client.unseal_multi(keys[1:3])
        assert result['sealed']
        assert result['progress'] == 2
        self.client.unseal_multi(keys[0:1])
        result = self.client.unseal_multi(keys[2:3])
        assert not result['sealed']

    def test_seal_unseal(self):
        cls = type(self)

        assert not self.client.is_sealed()

        self.client.seal()

        assert self.client.is_sealed()

        cls.manager.unseal()

        assert not self.client.is_sealed()

    def test_ha_status(self):
        assert 'ha_enabled' in self.client.ha_status

    def test_generic_secret_backend(self):
        self.client.write('secret/foo', zap='zip')
        result = self.client.read('secret/foo')

        assert result['data']['zap'] == 'zip'

        self.client.delete('secret/foo')

    def test_list_directory(self):
        self.client.write('secret/test-list/bar/foo', value='bar')
        self.client.write('secret/test-list/foo', value='bar')
        result = self.client.list('secret/test-list')

        assert result['data']['keys'] == ['bar/', 'foo']

        self.client.delete('secret/test-list/bar/foo')
        self.client.delete('secret/test-list/foo')

    def test_write_with_response(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        plaintext = 'test'

        self.client.write('transit/keys/foo')

        result = self.client.write('transit/encrypt/foo', plaintext=plaintext)
        ciphertext = result['data']['ciphertext']

        result = self.client.write('transit/decrypt/foo', ciphertext=ciphertext)
        assert result['data']['plaintext'] == plaintext

    def test_wrap_write(self):
        if 'approle/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend("approle")

        self.client.write("auth/approle/role/testrole")
        result = self.client.write('auth/approle/role/testrole/secret-id', wrap_ttl="10s")
        assert 'token' in result['wrap_info']
        self.client.unwrap(result['wrap_info']['token'])
        self.client.disable_auth_backend("approle")

    def test_read_nonexistent_key(self):
        assert not self.client.read('secret/I/dont/exist')

    def test_auth_backend_manipulation(self):
        assert 'github/' not in self.client.list_auth_backends()

        self.client.enable_auth_backend('github')
        assert 'github/' in self.client.list_auth_backends()

        self.client.token = self.root_token()
        self.client.disable_auth_backend('github')
        assert 'github/' not in self.client.list_auth_backends()

    def test_secret_backend_manipulation(self):
        assert 'test/' not in self.client.list_secret_backends()

        self.client.enable_secret_backend('generic', mount_point='test')
        assert 'test/' in self.client.list_secret_backends()

        secret_backend_tuning = self.client.get_secret_backend_tuning('generic', mount_point='test')
        self.assertEqual(secret_backend_tuning['max_lease_ttl'], 2764800)
        self.assertEqual(secret_backend_tuning['default_lease_ttl'], 2764800)

        self.client.tune_secret_backend('generic', mount_point='test', default_lease_ttl='3600s', max_lease_ttl='8600s')
        secret_backend_tuning = self.client.get_secret_backend_tuning('generic', mount_point='test')

        assert 'max_lease_ttl' in secret_backend_tuning
        self.assertEqual(secret_backend_tuning['max_lease_ttl'], 8600)
        assert 'default_lease_ttl' in secret_backend_tuning
        self.assertEqual(secret_backend_tuning['default_lease_ttl'], 3600)

        self.client.remount_secret_backend('test', 'foobar')
        assert 'test/' not in self.client.list_secret_backends()
        assert 'foobar/' in self.client.list_secret_backends()

        self.client.token = self.root_token()
        self.client.disable_secret_backend('foobar')
        assert 'foobar/' not in self.client.list_secret_backends()

    def test_audit_backend_manipulation(self):
        assert 'tmpfile/' not in self.client.list_audit_backends()

        options = {
            'path': '/tmp/vault.audit.log'
        }

        self.client.enable_audit_backend('file', options=options, name='tmpfile')
        assert 'tmpfile/' in self.client.list_audit_backends()

        self.client.token = self.root_token()
        self.client.disable_audit_backend('tmpfile')
        assert 'tmpfile/' not in self.client.list_audit_backends()

    def prep_policy(self, name):
        text = """
        path "sys" {
            policy = "deny"
        }
            path "secret" {
        policy = "write"
        }
        """
        obj = {
            'path': {
                'sys': {
                    'policy': 'deny'},
                'secret': {
                    'policy': 'write'}
            }
        }
        self.client.set_policy(name, text)
        return text, obj

    def test_policy_manipulation(self):
        assert 'root' in self.client.list_policies()
        assert self.client.get_policy('test') is None
        policy, parsed_policy = self.prep_policy('test')
        assert 'test' in self.client.list_policies()
        assert policy == self.client.get_policy('test')
        assert parsed_policy == self.client.get_policy('test', parse=True)

        self.client.delete_policy('test')
        assert 'test' not in self.client.list_policies()

    def test_json_policy_manipulation(self):
        assert 'root' in self.client.list_policies()

        policy = {
            "path": {
                "sys": {
                    "policy": "deny"
                },
                "secret": {
                    "policy": "write"
                }
            }
        }

        self.client.set_policy('test', policy)
        assert 'test' in self.client.list_policies()

        self.client.delete_policy('test')
        assert 'test' not in self.client.list_policies()

    def test_auth_token_manipulation(self):
        result = self.client.create_token(lease='1h', renewable=True)
        assert result['auth']['client_token']

        lookup = self.client.lookup_token(result['auth']['client_token'])
        assert result['auth']['client_token'] == lookup['data']['id']

        renew = self.client.renew_token(lookup['data']['id'])
        assert result['auth']['client_token'] == renew['auth']['client_token']

        self.client.revoke_token(lookup['data']['id'])

        try:
            lookup = self.client.lookup_token(result['auth']['client_token'])
            assert False
        except exceptions.Forbidden:
            assert True
        except exceptions.InvalidPath:
            assert True
        except exceptions.InvalidRequest:
            assert True

    def test_userpass_auth(self):
        if 'userpass/' in self.client.list_auth_backends():
            self.client.disable_auth_backend('userpass')

        self.client.enable_auth_backend('userpass')

        self.client.write('auth/userpass/users/testuser', password='testpass', policies='not_root')

        result = self.client.auth_userpass('testuser', 'testpass')

        assert self.client.token == result['auth']['client_token']
        assert self.client.is_authenticated()

        self.client.token = self.root_token()
        self.client.disable_auth_backend('userpass')

    def test_create_userpass(self):
        if 'userpass/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend('userpass')

        self.client.create_userpass('testcreateuser', 'testcreateuserpass', policies='not_root')

        result = self.client.auth_userpass('testcreateuser', 'testcreateuserpass')

        assert self.client.token == result['auth']['client_token']
        assert self.client.is_authenticated()

        # Test ttl:
        self.client.token = self.root_token()
        self.client.create_userpass('testcreateuser', 'testcreateuserpass', policies='not_root', ttl='10s')
        self.client.token = result['auth']['client_token']

        result = self.client.auth_userpass('testcreateuser', 'testcreateuserpass')

        assert result['auth']['lease_duration'] == 10

        self.client.token = self.root_token()
        self.client.disable_auth_backend('userpass')

    def test_list_userpass(self):
        if 'userpass/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend('userpass')

        # add some users and confirm that they show up in the list
        self.client.create_userpass('testuserone', 'testuseronepass', policies='not_root')
        self.client.create_userpass('testusertwo', 'testusertwopass', policies='not_root')

        user_list = self.client.list_userpass()
        assert 'testuserone' in user_list['data']['keys']
        assert 'testusertwo' in user_list['data']['keys']

        # delete all the users and confirm that list_userpass() doesn't fail
        for user in user_list['data']['keys']:
            self.client.delete_userpass(user)

        no_users_list = self.client.list_userpass()
        assert no_users_list is None

    def test_read_userpass(self):
        if 'userpass/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend('userpass')

        # create user to read
        self.client.create_userpass('readme', 'mypassword', policies='not_root')

        # test that user can be read
        read_user = self.client.read_userpass('readme')
        assert 'not_root' in read_user['data']['policies']

        # teardown
        self.client.disable_auth_backend('userpass')

    def test_update_userpass_policies(self):
        if 'userpass/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend('userpass')

        # create user and then update its policies
        self.client.create_userpass('updatemypolicies', 'mypassword', policies='not_root')
        self.client.update_userpass_policies('updatemypolicies', policies='somethingelse')

        # test that policies have changed
        updated_user = self.client.read_userpass('updatemypolicies')
        assert 'somethingelse' in updated_user['data']['policies']

        # teardown
        self.client.disable_auth_backend('userpass')

    def test_update_userpass_password(self):
        if 'userpass/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend('userpass')

        # create user and then change its password
        self.client.create_userpass('changeme', 'mypassword', policies='not_root')
        self.client.update_userpass_password('changeme', 'mynewpassword')

        # test that new password authenticates user
        result = self.client.auth_userpass('changeme', 'mynewpassword')
        assert self.client.token == result['auth']['client_token']
        assert self.client.is_authenticated()

        # teardown
        self.client.token = self.root_token()
        self.client.disable_auth_backend('userpass')

    def test_delete_userpass(self):
        if 'userpass/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend('userpass')

        self.client.create_userpass('testcreateuser', 'testcreateuserpass', policies='not_root')

        result = self.client.auth_userpass('testcreateuser', 'testcreateuserpass')

        assert self.client.token == result['auth']['client_token']
        assert self.client.is_authenticated()

        self.client.token = self.root_token()
        self.client.delete_userpass('testcreateuser')
        self.assertRaises(exceptions.InvalidRequest, self.client.auth_userpass, 'testcreateuser', 'testcreateuserpass')

    def test_app_id_auth(self):
        if 'app-id/' in self.client.list_auth_backends():
            self.client.disable_auth_backend('app-id')

        self.client.enable_auth_backend('app-id')

        self.client.write('auth/app-id/map/app-id/foo', value='not_root')
        self.client.write('auth/app-id/map/user-id/bar', value='foo')

        result = self.client.auth_app_id('foo', 'bar')

        assert self.client.token == result['auth']['client_token']
        assert self.client.is_authenticated()

        self.client.token = self.root_token()
        self.client.disable_auth_backend('app-id')

    def test_create_app_id(self):
        if 'app-id/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend('app-id')

        self.client.create_app_id('testappid', policies='not_root', display_name='displayname')

        result = self.client.read('auth/app-id/map/app-id/testappid')
        lib_result = self.client.get_app_id('testappid')
        del result['request_id']
        del lib_result['request_id']
        assert result == lib_result

        assert result['data']['key'] == 'testappid'
        assert result['data']['display_name'] == 'displayname'
        assert result['data']['value'] == 'not_root'
        self.client.delete_app_id('testappid')
        assert self.client.get_app_id('testappid')['data'] is None

        self.client.token = self.root_token()
        self.client.disable_auth_backend('app-id')

    def test_cubbyhole_auth(self):
        orig_token = self.client.token

        resp = self.client.create_token(lease='6h', wrap_ttl='1h')
        assert resp['wrap_info']['ttl'] == 3600

        wrapped_token = resp['wrap_info']['token']
        self.client.auth_cubbyhole(wrapped_token)
        assert self.client.token != orig_token
        assert self.client.token != wrapped_token
        assert self.client.is_authenticated()

        self.client.token = orig_token
        assert self.client.is_authenticated()

    def test_create_user_id(self):
        if 'app-id/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend('app-id')

        self.client.create_app_id('testappid', policies='not_root', display_name='displayname')
        self.client.create_user_id('testuserid', app_id='testappid')

        result = self.client.read('auth/app-id/map/user-id/testuserid')
        lib_result = self.client.get_user_id('testuserid')
        del result['request_id']
        del lib_result['request_id']
        assert result == lib_result

        assert result['data']['key'] == 'testuserid'
        assert result['data']['value'] == 'testappid'

        result = self.client.auth_app_id('testappid', 'testuserid')

        assert self.client.token == result['auth']['client_token']
        assert self.client.is_authenticated()
        self.client.token = self.root_token()
        self.client.delete_user_id('testuserid')
        assert self.client.get_user_id('testuserid')['data'] is None

        self.client.token = self.root_token()
        self.client.disable_auth_backend('app-id')

    def test_create_role(self):
        if 'approle/' in self.client.list_auth_backends():
            self.client.disable_auth_backend('approle')
        self.client.enable_auth_backend('approle')

        self.client.create_role('testrole')

        result = self.client.read('auth/approle/role/testrole')
        lib_result = self.client.get_role('testrole')
        del result['request_id']
        del lib_result['request_id']

        assert result == lib_result
        self.client.token = self.root_token()
        self.client.disable_auth_backend('approle')

    def test_delete_role(self):
        test_role_name = 'test-role'
        if 'approle/' in self.client.list_auth_backends():
            self.client.disable_auth_backend('approle')
        self.client.enable_auth_backend('approle')

        self.client.create_role(test_role_name)
        # We add a second dummy test role so we can still hit the /role?list=true route after deleting the first role
        self.client.create_role('test-role-2')

        # Ensure our created role shows up when calling list_roles as expected
        result = self.client.list_roles()
        actual_list_role_keys = result['data']['keys']
        self.assertIn(
            member=test_role_name,
            container=actual_list_role_keys,
        )

        # Now delete the role and verify its absence when calling list_roles
        self.client.delete_role(test_role_name)
        result = self.client.list_roles()
        actual_list_role_keys = result['data']['keys']
        self.assertNotIn(
            member=test_role_name,
            container=actual_list_role_keys,
        )

        # reset test environment
        self.client.token = self.root_token()
        self.client.disable_auth_backend('approle')

    def test_create_delete_role_secret_id(self):
        if 'approle/' in self.client.list_auth_backends():
            self.client.disable_auth_backend('approle')
        self.client.enable_auth_backend('approle')

        self.client.create_role('testrole')
        create_result = self.client.create_role_secret_id('testrole', {'foo': 'bar'})
        secret_id = create_result['data']['secret_id']
        result = self.client.get_role_secret_id('testrole', secret_id)
        assert result['data']['metadata']['foo'] == 'bar'
        self.client.delete_role_secret_id('testrole', secret_id)
        try:
            self.client.get_role_secret_id('testrole', secret_id)
            assert False
        except (exceptions.InvalidPath, ValueError):
            assert True
        self.client.token = self.root_token()
        self.client.disable_auth_backend('approle')

    def test_auth_approle(self):
        if 'approle/' in self.client.list_auth_backends():
            self.client.disable_auth_backend('approle')
        self.client.enable_auth_backend('approle')

        self.client.create_role('testrole')
        create_result = self.client.create_role_secret_id('testrole', {'foo': 'bar'})
        secret_id = create_result['data']['secret_id']
        role_id = self.client.get_role_id('testrole')
        result = self.client.auth_approle(role_id, secret_id)
        assert result['auth']['metadata']['foo'] == 'bar'
        assert self.client.token == result['auth']['client_token']
        assert self.client.is_authenticated()
        self.client.token = self.root_token()
        self.client.disable_auth_backend('approle')

    def test_auth_approle_dont_use_token(self):
        if 'approle/' in self.client.list_auth_backends():
            self.client.disable_auth_backend('approle')
        self.client.enable_auth_backend('approle')

        self.client.create_role('testrole')
        create_result = self.client.create_role_secret_id('testrole', {'foo': 'bar'})
        secret_id = create_result['data']['secret_id']
        role_id = self.client.get_role_id('testrole')
        result = self.client.auth_approle(role_id, secret_id, use_token=False)
        assert result['auth']['metadata']['foo'] == 'bar'
        assert self.client.token != result['auth']['client_token']
        self.client.token = self.root_token()
        self.client.disable_auth_backend('approle')

    def test_transit_read_write(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo')
        result = self.client.transit_read_key('foo')
        assert not result['data']['exportable']

        self.client.transit_create_key('foo_export', exportable=True, key_type="ed25519")
        result = self.client.transit_read_key('foo_export')
        assert result['data']['exportable']
        assert result['data']['type'] == 'ed25519'

        self.client.enable_secret_backend('transit', mount_point='bar')
        self.client.transit_create_key('foo', mount_point='bar')
        result = self.client.transit_read_key('foo', mount_point='bar')
        assert not result['data']['exportable']

    def test_transit_list_keys(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo1')
        self.client.transit_create_key('foo2')
        self.client.transit_create_key('foo3')

        result = self.client.transit_list_keys()
        assert result['data']['keys'] == ["foo1", "foo2", "foo3"]

    def test_transit_update_delete_keys(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo')
        self.client.transit_update_key('foo', deletion_allowed=True)
        result = self.client.transit_read_key('foo')
        assert result['data']['deletion_allowed']

        self.client.transit_delete_key('foo')

        try:
            self.client.transit_read_key('foo')
        except exceptions.InvalidPath:
            assert True
        else:
            assert False

    def test_transit_rotate_key(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo')

        self.client.transit_rotate_key('foo')
        response = self.client.transit_read_key('foo')
        assert '2' in response['data']['keys']

        self.client.transit_rotate_key('foo')
        response = self.client.transit_read_key('foo')
        assert '3' in response['data']['keys']

    def test_transit_export_key(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo', exportable=True)
        response = self.client.transit_export_key('foo', key_type='encryption-key')
        assert response is not None

    def test_transit_encrypt_data(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo')
        ciphertext_resp = self.client.transit_encrypt_data('foo', 'abbaabba')['data']['ciphertext']
        plaintext_resp = self.client.transit_decrypt_data('foo', ciphertext_resp)['data']['plaintext']
        assert plaintext_resp == 'abbaabba'

    def test_transit_rewrap_data(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo')
        ciphertext_resp = self.client.transit_encrypt_data('foo', 'abbaabba')['data']['ciphertext']

        self.client.transit_rotate_key('foo')
        response_wrap = self.client.transit_rewrap_data('foo', ciphertext=ciphertext_resp)['data']['ciphertext']
        plaintext_resp = self.client.transit_decrypt_data('foo', response_wrap)['data']['plaintext']
        assert plaintext_resp == 'abbaabba'

    def test_transit_generate_data_key(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo')

        response_plaintext = self.client.transit_generate_data_key('foo', key_type='plaintext')['data']['plaintext']
        assert response_plaintext

        response_ciphertext = self.client.transit_generate_data_key('foo', key_type='wrapped')['data']
        assert 'ciphertext' in response_ciphertext
        assert 'plaintext' not in response_ciphertext

    def test_transit_generate_rand_bytes(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        response_data = self.client.transit_generate_rand_bytes(data_bytes=4)['data']['random_bytes']
        assert response_data

    def test_transit_hash_data(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        response_hash = self.client.transit_hash_data('abbaabba')['data']['sum']
        assert len(response_hash) == 64

        response_hash = self.client.transit_hash_data('abbaabba', algorithm="sha2-512")['data']['sum']
        assert len(response_hash) == 128

    def test_transit_generate_verify_hmac(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo')

        response_hmac = self.client.transit_generate_hmac('foo', 'abbaabba')['data']['hmac']
        assert response_hmac
        verify_resp = self.client.transit_verify_signed_data('foo', 'abbaabba', hmac=response_hmac)['data']['valid']
        assert verify_resp

        response_hmac = self.client.transit_generate_hmac('foo', 'abbaabba', algorithm='sha2-512')['data']['hmac']
        assert response_hmac
        verify_resp = self.client.transit_verify_signed_data('foo', 'abbaabba',
                                                             algorithm='sha2-512', hmac=response_hmac)['data']['valid']
        assert verify_resp

    def test_transit_sign_verify_signature_data(self):
        if 'transit/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('transit')
        self.client.enable_secret_backend('transit')

        self.client.transit_create_key('foo', key_type='ed25519')

        signed_resp = self.client.transit_sign_data('foo', 'abbaabba')['data']['signature']
        assert signed_resp
        verify_resp = self.client.transit_verify_signed_data('foo', 'abbaabba', signature=signed_resp)['data']['valid']
        assert verify_resp

        signed_resp = self.client.transit_sign_data('foo', 'abbaabba', algorithm='sha2-512')['data']['signature']
        assert signed_resp
        verify_resp = self.client.transit_verify_signed_data('foo', 'abbaabba',
                                                             algorithm='sha2-512',
                                                             signature=signed_resp)['data']['valid']
        assert verify_resp

    def test_missing_token(self):
        client = create_client()
        assert not client.is_authenticated()

    def test_invalid_token(self):
        client = create_client(token='not-a-real-token')
        assert not client.is_authenticated()

    def test_illegal_token(self):
        client = create_client(token='token-with-new-line\n')
        try:
            client.is_authenticated()
        except ValueError as e:
            assert 'Invalid header value' in str(e)

    def test_broken_token(self):
        client = create_client(token='\x1b')
        try:
            client.is_authenticated()
        except exceptions.InvalidRequest as e:
            assert "invalid header value" in str(e)

    def test_client_authenticated(self):
        assert self.client.is_authenticated()

    def test_client_logout(self):
        self.client.logout()
        assert not self.client.is_authenticated()

    def test_revoke_self_token(self):
        if 'userpass/' in self.client.list_auth_backends():
            self.client.disable_auth_backend('userpass')

        self.client.enable_auth_backend('userpass')

        self.client.write('auth/userpass/users/testuser', password='testpass', policies='not_root')

        self.client.auth_userpass('testuser', 'testpass')

        self.client.revoke_self_token()
        assert not self.client.is_authenticated()

    def test_rekey_multi(self):
        cls = type(self)

        assert not self.client.rekey_status['started']

        self.client.start_rekey()
        assert self.client.rekey_status['started']

        self.client.cancel_rekey()
        assert not self.client.rekey_status['started']

        result = self.client.start_rekey()

        keys = cls.manager.keys

        result = self.client.rekey_multi(keys, nonce=result['nonce'])
        assert result['complete']

        cls.manager.keys = result['keys']
        cls.manager.unseal()

    def test_rotate(self):
        status = self.client.key_status

        self.client.rotate()

        assert self.client.key_status['term'] > status['term']

    def test_tls_auth(self):
        self.client.enable_auth_backend('cert')

        with open('test/client-cert.pem') as fp:
            certificate = fp.read()

        self.client.write('auth/cert/certs/test', display_name='test',
                          policies='not_root', certificate=certificate)

        self.client.auth_tls()

    def test_gh51(self):
        key = 'secret/http://test.com'

        self.client.write(key, foo='bar')

        result = self.client.read(key)

        assert result['data']['foo'] == 'bar'

    def test_token_accessor(self):
        # Create token, check accessor is provided
        result = self.client.create_token(lease='1h')
        token_accessor = result['auth'].get('accessor', None)
        assert token_accessor

        # Look up token by accessor, make sure token is excluded from results
        lookup = self.client.lookup_token(token_accessor, accessor=True)
        assert lookup['data']['accessor'] == token_accessor
        assert not lookup['data']['id']

        # Revoke token using the accessor
        self.client.revoke_token(token_accessor, accessor=True)

        # Look up by accessor should fail
        with self.assertRaises(exceptions.InvalidRequest):
            lookup = self.client.lookup_token(token_accessor, accessor=True)

        # As should regular lookup
        with self.assertRaises(exceptions.Forbidden):
            lookup = self.client.lookup_token(result['auth']['client_token'])

    def test_wrapped_token_success(self):
        wrap = self.client.create_token(wrap_ttl='1m')

        # Unwrap token
        result = self.client.unwrap(wrap['wrap_info']['token'])
        assert result['auth']['client_token']

        # Validate token
        lookup = self.client.lookup_token(result['auth']['client_token'])
        assert result['auth']['client_token'] == lookup['data']['id']

    def test_wrapped_token_intercept(self):
        wrap = self.client.create_token(wrap_ttl='1m')

        # Intercept wrapped token
        self.client.unwrap(wrap['wrap_info']['token'])

        # Attempt to retrieve the token after it's been intercepted
        with self.assertRaises(exceptions.InvalidRequest):
            self.client.unwrap(wrap['wrap_info']['token'])

    def test_wrapped_token_cleanup(self):
        wrap = self.client.create_token(wrap_ttl='1m')

        _token = self.client.token
        self.client.unwrap(wrap['wrap_info']['token'])
        assert self.client.token == _token

    def test_wrapped_token_revoke(self):
        wrap = self.client.create_token(wrap_ttl='1m')

        # Revoke token before it's unwrapped
        self.client.revoke_token(wrap['wrap_info']['wrapped_accessor'], accessor=True)

        # Unwrap token anyway
        result = self.client.unwrap(wrap['wrap_info']['token'])
        assert result['auth']['client_token']

        # Attempt to validate token
        with self.assertRaises(exceptions.Forbidden):
            self.client.lookup_token(result['auth']['client_token'])

    def test_wrapped_client_token_success(self):
        wrap = self.client.create_token(wrap_ttl='1m')
        self.client.token = wrap['wrap_info']['token']

        # Unwrap token
        result = self.client.unwrap()
        assert result['auth']['client_token']

        # Validate token
        self.client.token = result['auth']['client_token']
        lookup = self.client.lookup_token(result['auth']['client_token'])
        assert result['auth']['client_token'] == lookup['data']['id']

    def test_wrapped_client_token_intercept(self):
        wrap = self.client.create_token(wrap_ttl='1m')
        self.client.token = wrap['wrap_info']['token']

        # Intercept wrapped token
        self.client.unwrap()

        # Attempt to retrieve the token after it's been intercepted
        with self.assertRaises(exceptions.InvalidRequest):
            self.client.unwrap()

    def test_wrapped_client_token_cleanup(self):
        wrap = self.client.create_token(wrap_ttl='1m')

        _token = self.client.token
        self.client.token = wrap['wrap_info']['token']
        self.client.unwrap()

        assert self.client.token != wrap
        assert self.client.token != _token

    def test_wrapped_client_token_revoke(self):
        wrap = self.client.create_token(wrap_ttl='1m')

        # Revoke token before it's unwrapped
        self.client.revoke_token(wrap['wrap_info']['wrapped_accessor'], accessor=True)

        # Unwrap token anyway
        self.client.token = wrap['wrap_info']['token']
        result = self.client.unwrap()
        assert result['auth']['client_token']

        # Attempt to validate token
        with self.assertRaises(exceptions.Forbidden):
            self.client.lookup_token(result['auth']['client_token'])

    def test_create_token_explicit_max_ttl(self):

        token = self.client.create_token(ttl='30m', explicit_max_ttl='5m')

        assert token['auth']['client_token']

        assert token['auth']['lease_duration'] == 300

        # Validate token
        lookup = self.client.lookup_token(token['auth']['client_token'])
        assert token['auth']['client_token'] == lookup['data']['id']

    def test_create_token_max_ttl(self):

        token = self.client.create_token(ttl='5m')

        assert token['auth']['client_token']

        assert token['auth']['lease_duration'] == 300

        # Validate token
        lookup = self.client.lookup_token(token['auth']['client_token'])
        assert token['auth']['client_token'] == lookup['data']['id']

    def test_create_token_periodic(self):

        token = self.client.create_token(period='30m')

        assert token['auth']['client_token']

        assert token['auth']['lease_duration'] == 1800

        # Validate token
        lookup = self.client.lookup_token(token['auth']['client_token'])
        assert token['auth']['client_token'] == lookup['data']['id']
        assert lookup['data']['period'] == 1800

    def test_token_roles(self):
        # No roles, list_token_roles == None
        before = self.client.list_token_roles()
        assert not before

        # Create token role
        assert self.client.create_token_role('testrole').status_code == 204

        # List token roles
        during = self.client.list_token_roles()['data']['keys']
        assert len(during) == 1
        assert during[0] == 'testrole'

        # Delete token role
        self.client.delete_token_role('testrole')

        # No roles, list_token_roles == None
        after = self.client.list_token_roles()
        assert not after

    def test_create_token_w_role(self):
        # Create policy
        self.prep_policy('testpolicy')

        # Create token role w/ policy
        assert self.client.create_token_role('testrole',
                                             allowed_policies='testpolicy').status_code == 204

        # Create token against role
        token = self.client.create_token(lease='1h', role='testrole')
        assert token['auth']['client_token']
        assert token['auth']['policies'] == ['default', 'testpolicy']

        # Cleanup
        self.client.delete_token_role('testrole')
        self.client.delete_policy('testpolicy')

    def test_ec2_role_crud(self):
        if 'aws-ec2/' in self.client.list_auth_backends():
            self.client.disable_auth_backend('aws-ec2')
        self.client.enable_auth_backend('aws-ec2')

        # create a policy to associate with the role
        self.prep_policy('ec2rolepolicy')

        # attempt to get a list of roles before any exist
        no_roles = self.client.list_ec2_roles()
        # doing so should succeed and return None
        assert (no_roles is None)

        # test binding by AMI ID (the old way, to ensure backward compatibility)
        self.client.create_ec2_role('foo',
                                    'ami-notarealami',
                                    policies='ec2rolepolicy')

        # test binding by Account ID
        self.client.create_ec2_role('bar',
                                    bound_account_id='123456789012',
                                    policies='ec2rolepolicy')

        # test binding by IAM Role ARN
        self.client.create_ec2_role('baz',
                                    bound_iam_role_arn='arn:aws:iam::123456789012:role/mockec2role',
                                    policies='ec2rolepolicy')

        # test binding by instance profile ARN
        self.client.create_ec2_role('qux',
                                    bound_iam_instance_profile_arn='arn:aws:iam::123456789012:instance-profile/mockprofile',
                                    policies='ec2rolepolicy')

        # test binding by bound region
        self.client.create_ec2_role('quux',
                                    bound_region='ap-northeast-2',
                                    policies='ec2rolepolicy')

        # test binding by bound vpc id
        self.client.create_ec2_role('corge',
                                    bound_vpc_id='vpc-1a123456',
                                    policies='ec2rolepolicy')

        # test binding by bound subnet id
        self.client.create_ec2_role('grault',
                                    bound_subnet_id='subnet-123a456',
                                    policies='ec2rolepolicy')

        roles = self.client.list_ec2_roles()

        assert('foo' in roles['data']['keys'])
        assert('bar' in roles['data']['keys'])
        assert('baz' in roles['data']['keys'])
        assert('qux' in roles['data']['keys'])
        assert('quux' in roles['data']['keys'])
        assert('corge' in roles['data']['keys'])
        assert('grault' in roles['data']['keys'])

        foo_role = self.client.get_ec2_role('foo')
        assert ('ami-notarealami' in foo_role['data']['bound_ami_id'])
        assert ('ec2rolepolicy' in foo_role['data']['policies'])

        bar_role = self.client.get_ec2_role('bar')
        assert ('123456789012' in bar_role['data']['bound_account_id'])
        assert ('ec2rolepolicy' in bar_role['data']['policies'])

        baz_role = self.client.get_ec2_role('baz')
        assert ('arn:aws:iam::123456789012:role/mockec2role' in baz_role['data']['bound_iam_role_arn'])
        assert ('ec2rolepolicy' in baz_role['data']['policies'])

        qux_role = self.client.get_ec2_role('qux')
        assert('arn:aws:iam::123456789012:instance-profile/mockprofile' in qux_role['data']['bound_iam_instance_profile_arn'])
        assert('ec2rolepolicy' in qux_role['data']['policies'])

        quux_role = self.client.get_ec2_role('quux')
        assert('ap-northeast-2' in quux_role['data']['bound_region'])
        assert('ec2rolepolicy' in quux_role['data']['policies'])

        corge_role = self.client.get_ec2_role('corge')
        assert('vpc-1a123456' in corge_role['data']['bound_vpc_id'])
        assert('ec2rolepolicy' in corge_role['data']['policies'])

        grault_role = self.client.get_ec2_role('grault')
        assert('subnet-123a456' in grault_role['data']['bound_subnet_id'])
        assert('ec2rolepolicy' in grault_role['data']['policies'])

        # teardown
        self.client.delete_ec2_role('foo')
        self.client.delete_ec2_role('bar')
        self.client.delete_ec2_role('baz')
        self.client.delete_ec2_role('qux')
        self.client.delete_ec2_role('quux')
        self.client.delete_ec2_role('corge')
        self.client.delete_ec2_role('grault')

        self.client.delete_policy('ec2rolepolicy')

        self.client.disable_auth_backend('aws-ec2')

    def test_ec2_role_token_lifespan(self):
        if 'aws-ec2/' not in self.client.list_auth_backends():
            self.client.enable_auth_backend('aws-ec2')

        # create a policy to associate with the role
        self.prep_policy('ec2rolepolicy')

        # create a role with no TTL
        self.client.create_ec2_role('foo',
                                    'ami-notarealami',
                                    policies='ec2rolepolicy')

        # create a role with a 1hr TTL
        self.client.create_ec2_role('bar',
                                    'ami-notarealami',
                                    ttl='1h',
                                    policies='ec2rolepolicy')

        # create a role with a 3-day max TTL
        self.client.create_ec2_role('baz',
                                    'ami-notarealami',
                                    max_ttl='72h',
                                    policies='ec2rolepolicy')

        # create a role with 1-day period
        self.client.create_ec2_role('qux',
                                    'ami-notarealami',
                                    period='24h',
                                    policies='ec2rolepolicy')

        foo_role = self.client.get_ec2_role('foo')
        assert (foo_role['data']['ttl'] == 0)

        bar_role = self.client.get_ec2_role('bar')
        assert (bar_role['data']['ttl'] == 3600)

        baz_role = self.client.get_ec2_role('baz')
        assert (baz_role['data']['max_ttl'] == 259200)

        qux_role = self.client.get_ec2_role('qux')
        assert (qux_role['data']['period'] == 86400)

        # teardown
        self.client.delete_ec2_role('foo')
        self.client.delete_ec2_role('bar')
        self.client.delete_ec2_role('baz')
        self.client.delete_ec2_role('qux')

        self.client.delete_policy('ec2rolepolicy')

        self.client.disable_auth_backend('aws-ec2')

    def test_start_generate_root_with_completion(self):
        test_otp = 'RSMGkAqBH5WnVLrDTbZ+UQ=='

        self.assertFalse(self.client.generate_root_status['started'])
        response = self.client.start_generate_root(
            key=test_otp,
            otp=True,
        )
        self.assertTrue(self.client.generate_root_status['started'])

        nonce = response['nonce']
        for key in self.manager.keys[0:3]:
            response = self.client.generate_root(
                key=key,
                nonce=nonce,
            )
        self.assertFalse(self.client.generate_root_status['started'])

        # Decode the token provided in the last response. Root token decoding logic derived from:
        # https://github.com/hashicorp/vault/blob/284600fbefc32d8ab71b6b9d1d226f2f83b56b1d/command/operator_generate_root.go#L289
        b64decoded_root_token = b64decode(response['encoded_root_token'])
        if sys.version_info > (3, 0):
            # b64decoding + bytes XOR'ing to decode the new root token in python 3.x
            int_encoded_token = int.from_bytes(b64decoded_root_token, sys.byteorder)
            int_otp = int.from_bytes(b64decode(test_otp), sys.byteorder)
            xord_otp_and_token = int_otp ^ int_encoded_token
            token_hex_string = xord_otp_and_token.to_bytes(len(b64decoded_root_token), sys.byteorder).hex()
        else:
            # b64decoding + bytes XOR'ing to decode the new root token in python 2.7
            otp_and_token = zip(b64decode(test_otp), b64decoded_root_token)
            xord_otp_and_token = ''.join(chr(ord(y) ^ ord(x)) for (x, y) in otp_and_token)
            token_hex_string = binascii.hexlify(xord_otp_and_token)

        new_root_token = str(UUID(token_hex_string))

        # Assert our new root token is properly formed and authenticated
        self.client.token = new_root_token
        if self.client.is_authenticated():
            self.root_token = new_root_token
        else:
            # If our new token was unable to authenticate, set the test client's token back to the original value
            self.client.token = self.root_token
            self.fail('Unable to authenticate with the newly generated root token.')

    def test_start_generate_root_then_cancel(self):
        test_otp = 'RSMGkAqBH5WnVLrDTbZ+UQ=='

        self.assertFalse(self.client.generate_root_status['started'])
        self.client.start_generate_root(
            key=test_otp,
            otp=True,
        )
        self.assertTrue(self.client.generate_root_status['started'])

        self.client.cancel_generate_root()
        self.assertFalse(self.client.generate_root_status['started'])

    def test_auth_ec2_alternate_mount_point_with_no_client_token_exception(self):
        test_mount_point = 'aws-custom-path'
        # Turn on the aws-ec2 backend with a custom mount_point path specified.
        if '{0}/'.format(test_mount_point) in self.client.list_auth_backends():
            self.client.disable_auth_backend(test_mount_point)
        self.client.enable_auth_backend('aws-ec2', mount_point=test_mount_point)

        # Drop the client's token to replicate a typical end user's use of any auth method.
        # I.e., its reasonable to expect the method is being called to _retrieve_ a token in the first place.
        self.client.token = None

        # Load a mock PKCS7 encoded self-signed certificate to stand in for a real document from the AWS identity service.
        with open('test/identity_document.p7b') as fp:
            pkcs7 = fp.read()

        # When attempting to auth (POST) to an auth backend mounted at a different path than the default, we expect a
        # generic 'missing client token' response from Vault.
        with self.assertRaises(exceptions.InvalidRequest) as assertRaisesContext:
            self.client.auth_ec2(pkcs7=pkcs7)

        expected_exception_message = 'missing client token'
        actual_exception_message = str(assertRaisesContext.exception)
        self.assertEqual(expected_exception_message, actual_exception_message)

        # Reset test state.
        self.client.token = self.root_token()
        self.client.disable_auth_backend(mount_point=test_mount_point)

    def test_auth_ec2_alternate_mount_point_with_no_client_token(self):
        test_mount_point = 'aws-custom-path'
        # Turn on the aws-ec2 backend with a custom mount_point path specified.
        if '{0}/'.format(test_mount_point) in self.client.list_auth_backends():
            self.client.disable_auth_backend(test_mount_point)
        self.client.enable_auth_backend('aws-ec2', mount_point=test_mount_point)

        # Drop the client's token to replicate a typical end user's use of any auth method.
        # I.e., its reasonable to expect the method is being called to _retrieve_ a token in the first place.
        self.client.token = None

        # Load a mock PKCS7 encoded self-signed certificate to stand in for a real document from the AWS identity service.
        with open('test/identity_document.p7b') as fp:
            pkcs7 = fp.read()

        # If our custom path is respected, we'll still end up with Vault's inability to decrypt our dummy PKCS7 string.
        # However this exception indicates we're correctly hitting the expected auth endpoint.
        with self.assertRaises(exceptions.InternalServerError) as assertRaisesContext:
            self.client.auth_ec2(pkcs7=pkcs7, mount_point=test_mount_point)

        expected_exception_message = 'failed to decode the PEM encoded PKCS#7 signature'
        actual_exception_message = str(assertRaisesContext.exception)
        self.assertEqual(expected_exception_message, actual_exception_message)

        # Reset test state.
        self.client.token = self.root_token()
        self.client.disable_auth_backend(mount_point=test_mount_point)

    def test_auth_gcp_alternate_mount_point_with_no_client_token_exception(self):
        test_mount_point = 'gcp-custom-path'
        # Turn on the gcp backend with a custom mount_point path specified.
        if '{0}/'.format(test_mount_point) in self.client.list_auth_backends():
            self.client.disable_auth_backend(test_mount_point)
        self.client.enable_auth_backend('gcp', mount_point=test_mount_point)

        # Drop the client's token to replicate a typical end user's use of any auth method.
        # I.e., its reasonable to expect the method is being called to _retrieve_ a token in the first place.
        self.client.token = None

        # Load a mock JWT stand in for a real document from GCP.
        with open('test/example.jwt') as fp:
            jwt = fp.read()

        # When attempting to auth (POST) to an auth backend mounted at a different path than the default, we expect a
        # generic 'missing client token' response from Vault.
        with self.assertRaises(exceptions.InvalidRequest) as assertRaisesContext:
            self.client.auth_gcp('example-role', jwt)

        expected_exception_message = 'missing client token'
        actual_exception_message = str(assertRaisesContext.exception)
        self.assertEqual(expected_exception_message, actual_exception_message)

        # Reset test state.
        self.client.token = self.root_token()
        self.client.disable_auth_backend(mount_point=test_mount_point)

    def test_tune_auth_backend(self):
        test_backend_type = 'approle'
        test_mount_point = 'tune-approle'
        test_description = 'this is a test auth backend'
        test_max_lease_ttl = 12345678
        if '{0}/'.format(test_mount_point) in self.client.list_auth_backends():
            self.client.disable_auth_backend(test_mount_point)
        self.client.enable_auth_backend(
            backend_type='approle',
            mount_point=test_mount_point
        )

        expected_status_code = 204
        response = self.client.tune_auth_backend(
            backend_type=test_backend_type,
            mount_point=test_mount_point,
            description=test_description,
            max_lease_ttl=test_max_lease_ttl,
        )
        self.assertEqual(
            first=expected_status_code,
            second=response.status_code,
        )

        response = self.client.get_auth_backend_tuning(
            backend_type=test_backend_type,
            mount_point=test_mount_point
        )

        self.assertEqual(
            first=test_max_lease_ttl,
            second=response['data']['max_lease_ttl']
        )

        self.client.disable_auth_backend(mount_point=test_mount_point)

    def test_kv2_secret_backend(self):
        if 'test/' in self.client.list_secret_backends():
            self.client.disable_secret_backend('test')
        self.client.enable_secret_backend('kv', mount_point='test', options={'version': '2'})

        secret_backends = self.client.list_secret_backends()

        assert 'test/' in secret_backends
        self.assertDictEqual(secret_backends['test/']['options'], {'version': '2'})

        self.client.disable_secret_backend('test')
