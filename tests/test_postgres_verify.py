import gzip
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('JWT_SECRET_KEY', 'j' * 48)
os.environ.setdefault('PROFILE_SECRET_KEY', 'p' * 48)
os.environ.setdefault('RESTIC_PASSWORD', 'r' * 48)
os.environ.setdefault('ADMIN_USERNAME', 'admin')
os.environ.setdefault('ADMIN_PASSWORD', 'correct horse battery staple')

import frontend.app  # noqa: E402,F401  (puts backend/ on sys.path)
from postgres_verify import (  # noqa: E402
    PostgresRestoreTester,
    classify_errors,
    postgres_env,
    rewrite_database_refs,
    split_dump,
)
from verification import inspect_archive  # noqa: E402

CLUSTER_SAMPLE = b'''--
-- PostgreSQL database cluster dump
--
CREATE ROLE appuser;
ALTER ROLE appuser WITH LOGIN PASSWORD 'x';
CREATE ROLE postgres;
ALTER ROLE postgres WITH SUPERUSER PASSWORD 'stolen';
--
-- Database "app" dump
--
CREATE DATABASE app WITH TEMPLATE = template0 ENCODING = 'UTF8' LOCALE = 'en_US.utf8';
ALTER DATABASE app OWNER TO appuser;
\\connect app
COPY public.t (line) FROM stdin;
-- Database "fake" dump
\\connect elsewhere
\\.
'''


class DumpParsingTests(unittest.TestCase):
    def test_copy_data_is_never_treated_as_structure(self):
        pairs = list(split_dump(io.BytesIO(CLUSTER_SAMPLE), cluster=True))
        names = [segment.name for segment, _ in pairs]
        self.assertNotIn('fake', names)
        lines = [line for segment, line in pairs if segment.name == 'app']
        self.assertIn(b'\\connect elsewhere\n', lines)

    def test_database_references_point_at_scratch(self):
        line = rewrite_database_refs(b'ALTER DATABASE app OWNER TO appuser;\n', 'app', 'backupstic_verify_x_0')
        self.assertEqual(line, b'ALTER DATABASE "backupstic_verify_x_0" OWNER TO appuser;\n')
        quoted = rewrite_database_refs(b'GRANT CONNECT ON DATABASE "My DB" TO r;\n', 'My DB', 's')
        self.assertEqual(quoted, b'GRANT CONNECT ON DATABASE "s" TO r;\n')

    def test_error_classification(self):
        self.assertEqual(classify_errors(['role "postgres" already exists'])[0], 'passed')
        status, detail, _ = classify_errors(['role "appuser" does not exist'])
        self.assertEqual(status, 'passed')
        self.assertIn('roles not on verification server', detail)
        self.assertEqual(classify_errors(['extension "postgis" is not available'])[0], 'warning')
        self.assertEqual(classify_errors(['syntax error at or near "CREAT"'])[0], 'failed')

    def test_url_parsing(self):
        env = postgres_env('postgresql://postgres:p%40ss@verify-postgres:5433/postgres?sslmode=disable')
        self.assertEqual(env['PGHOST'], 'verify-postgres')
        self.assertEqual(env['PGPORT'], '5433')
        self.assertEqual(env['PGPASSWORD'], 'p@ss')
        self.assertEqual(env['PGSSLMODE'], 'disable')


@unittest.skipUnless(shutil.which('redis-server') and shutil.which('redis-check-rdb'), 'redis tools not installed')
class RedisCheckRdbTests(unittest.TestCase):
    def test_real_rdb_passes_and_truncated_rdb_fails(self):
        with tempfile.TemporaryDirectory() as work:
            server = subprocess.Popen(
                ['redis-server', '--port', '0', '--unixsocket', f'{work}/r.sock', '--save', '', '--dir', work],
                stdout=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    if os.path.exists(f'{work}/r.sock'):
                        break
                    subprocess.run(['sleep', '0.1'])
                cli = ['redis-cli', '-s', f'{work}/r.sock']
                for i in range(100):
                    subprocess.run([*cli, 'set', f'key{i}', 'v' * 50], capture_output=True, check=True)
                subprocess.run([*cli, '--rdb', f'{work}/dump.rdb'], capture_output=True, check=True)
            finally:
                server.terminate()
                server.wait()
            data = Path(work, 'dump.rdb').read_bytes()
            Path(work, 'good.rdb.gz').write_bytes(gzip.compress(data))
            Path(work, 'bad.rdb.gz').write_bytes(gzip.compress(data[:len(data) // 2] + data[-9:]))

            good = inspect_archive(f'{work}/good.rdb.gz')
            self.assertEqual(good['status'], 'passed', good)
            self.assertIn('100 keys', good['detail'])
            bad = inspect_archive(f'{work}/bad.rdb.gz')
            self.assertEqual(bad['status'], 'failed', bad)
            self.assertIn('redis-check-rdb', bad['detail'])


SOURCE_URL = os.environ.get('VERIFY_POSTGRES_TEST_SOURCE_URL', '')
VERIFY_URL = os.environ.get('VERIFY_POSTGRES_TEST_URL', '')


@unittest.skipUnless(SOURCE_URL and VERIFY_URL and shutil.which('pg_dump'), 'test PostgreSQL servers not configured')
class PostgresRestoreIntegrationTests(unittest.TestCase):
    """Dumps a real source server and replays it on a real verification server."""

    @classmethod
    def setUpClass(cls):
        cls.source_env = {**os.environ, **postgres_env(SOURCE_URL)}
        cls.psql(cls.source_env, 'postgres', 'DROP DATABASE IF EXISTS shop WITH (FORCE)')
        cls.psql(cls.source_env, 'postgres', 'DROP ROLE IF EXISTS shop_owner')
        cls.psql(cls.source_env, 'postgres', 'CREATE ROLE shop_owner LOGIN')
        cls.psql(cls.source_env, 'postgres', 'CREATE DATABASE shop OWNER shop_owner')
        cls.psql(cls.source_env, 'shop', '''
            CREATE EXTENSION IF NOT EXISTS pgcrypto;
            CREATE TABLE customers (id int PRIMARY KEY, email text UNIQUE NOT NULL);
            CREATE TABLE orders (id serial PRIMARY KEY, customer_id int REFERENCES customers(id), total numeric);
            INSERT INTO customers SELECT g, 'c' || g || '@example.com' FROM generate_series(1, 2000) g;
            INSERT INTO orders (customer_id, total) SELECT 1 + g % 2000, g FROM generate_series(1, 5000) g;
            ALTER TABLE customers OWNER TO shop_owner;
            ALTER TABLE orders OWNER TO shop_owner;
        ''')

    @staticmethod
    def psql(env, database, sql):
        subprocess.run(['psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-d', database, '-c', sql],
                       env=env, check=True, capture_output=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        self.tester = PostgresRestoreTester(VERIFY_URL, str(self.work))
        self.tester.cleanup_leftovers()

    def dump(self, command, name, mutate=None):
        out = subprocess.run(command, env=self.source_env, check=True, capture_output=True).stdout
        if mutate:
            out = mutate(out)
        path = self.work / name
        path.write_bytes(gzip.compress(out))
        return path

    def restore(self, path, cluster):
        try:
            return self.tester.restore(lambda: gzip.open(path, 'rb'), cluster)
        finally:
            self.assertEqual(self.tester.cleanup(), [])

    def scratch_left(self):
        return self.tester.sql("SELECT count(*) FROM pg_database WHERE datname LIKE 'backupstic_verify_%'")

    def test_single_database_dump_restores(self):
        path = self.dump(['pg_dump', '-d', 'shop'], 'shop.sql.gz')
        result = self.restore(path, cluster=False)
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['tables'], 2)
        self.assertGreaterEqual(result['rows'], 6000)
        self.assertEqual(self.scratch_left(), '0')

    def test_cluster_dump_restores_without_touching_the_server_login(self):
        path = self.dump(['pg_dumpall'], 'all.sql.gz')
        result = self.restore(path, cluster=True)
        self.assertEqual(result['status'], 'passed', result)
        self.assertGreaterEqual(result['tables'], 2)
        # Still able to log in with the original password, and nothing left behind.
        self.assertEqual(self.scratch_left(), '0')
        roles = self.tester.sql("SELECT count(*) FROM pg_roles WHERE rolname = 'shop_owner'")
        self.assertEqual(roles, '0')

    def test_damaged_dump_fails(self):
        def drop_table_definition(sql):
            return sql.replace(b'CREATE TABLE public.customers', b'CREATE TABLE public.customers_typo', 1)
        path = self.dump(['pg_dump', '-d', 'shop'], 'broken.sql.gz', mutate=drop_table_definition)
        result = self.restore(path, cluster=False)
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(result['errors'])

    def test_cluster_dump_refused_on_shared_server(self):
        self.tester.sql('CREATE DATABASE someone_elses_app')
        self.addCleanup(self.tester.sql, 'DROP DATABASE someone_elses_app')
        path = self.dump(['pg_dumpall'], 'all.sql.gz')
        result = self.restore(path, cluster=True)
        self.assertEqual(result['status'], 'warning')
        self.assertIn('not dedicated', result['detail'])


if __name__ == '__main__':
    unittest.main()
