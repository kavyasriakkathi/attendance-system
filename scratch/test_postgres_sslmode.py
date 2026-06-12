import psycopg2
import sys

def test():
    # We don't have a running postgres locally, but we can verify if psycopg2 parser
    # accepts the options without throwing parse/validation errors.
    dummy_url = "postgresql://user:pass@localhost:5432/dbname?sslmode=require"
    try:
        # We expect a connection failure (OperationalError), but we want to make sure
        # it is "connection refused" or similar, NOT a parameter/argument parser error.
        print("Testing psycopg2 connection parser...")
        conn = psycopg2.connect(
            dummy_url,
            sslmode="require",
            connect_timeout=2
        )
    except psycopg2.OperationalError as e:
        err_msg = str(e)
        print("Caught OperationalError as expected:", err_msg)
        if "invalid connection option" in err_msg or "extra_float_digits" in err_msg or "cannot be specified multiple times" in err_msg:
            print("FAILED: Parser rejected duplicate parameters.")
        else:
            print("SUCCESS: Parser accepted parameters, failed only on connection host/port.")
    except Exception as e:
        print("Caught unexpected exception:", repr(e))

if __name__ == "__main__":
    test()
