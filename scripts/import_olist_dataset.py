from pathlib import Path
import argparse
import sys

import pandas as pd
from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    Numeric,
    SmallInteger,
    Unicode,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.dialects.mssql import NVARCHAR
from sqlalchemy.types import Float

from app.config import get_settings
from app.db.connection import make_engine


DATA_DIR = Path("docs") / "E-Commerce Dataset"
TARGET_DB = "dataops_ecommerce_demo"
CHUNK_SIZE = 10_000

# Each CSV is mapped to an explicit destination table and schema.
FILES = {
    "olist_customers_dataset.csv": ("ecom_customers", {
        "customer_id": NVARCHAR(32),
        "customer_unique_id": NVARCHAR(32),
        "customer_zip_code_prefix": NVARCHAR(10),
        "customer_city": Unicode(100),
        "customer_state": NVARCHAR(2),
    }),
    "olist_geolocation_dataset.csv": ("ecom_geolocation", {
        "geolocation_zip_code_prefix": NVARCHAR(10),
        "geolocation_lat": Numeric(10, 7),
        "geolocation_lng": Numeric(10, 7),
        "geolocation_city": Unicode(100),
        "geolocation_state": NVARCHAR(2),
    }),
    "olist_orders_dataset.csv": ("ecom_orders", {
        "order_id": NVARCHAR(32),
        "customer_id": NVARCHAR(32),
        "order_status": NVARCHAR(30),
        "order_purchase_timestamp": DateTime(),
        "order_approved_at": DateTime(),
        "order_delivered_carrier_date": DateTime(),
        "order_delivered_customer_date": DateTime(),
        "order_estimated_delivery_date": DateTime(),
    }),
    "olist_order_items_dataset.csv": ("ecom_order_items", {
        "order_id": NVARCHAR(32),
        "order_item_id": Integer(),
        "product_id": NVARCHAR(32),
        "seller_id": NVARCHAR(32),
        "shipping_limit_date": DateTime(),
        "price": Numeric(12, 2),
        "freight_value": Numeric(12, 2),
    }),
    "olist_order_payments_dataset.csv": ("ecom_order_payments", {
        "order_id": NVARCHAR(32),
        "payment_sequential": Integer(),
        "payment_type": NVARCHAR(30),
        "payment_installments": Integer(),
        "payment_value": Numeric(12, 2),
    }),
    "olist_order_reviews_dataset.csv": ("ecom_order_reviews", {
        "review_id": NVARCHAR(32),
        "order_id": NVARCHAR(32),
        "review_score": SmallInteger(),
        "review_comment_title": Unicode(2000),
        "review_comment_message": Unicode(4000),
        "review_creation_date": DateTime(),
        "review_answer_timestamp": DateTime(),
    }),
    "olist_products_dataset.csv": ("ecom_products", {
        "product_id": NVARCHAR(32),
        "product_category_name": Unicode(100),
        "product_name_lenght": Integer(),
        "product_description_lenght": Integer(),
        "product_photos_qty": Integer(),
        "product_weight_g": Integer(),
        "product_length_cm": Integer(),
        "product_height_cm": Integer(),
        "product_width_cm": Integer(),
    }),
    "olist_sellers_dataset.csv": ("ecom_sellers", {
        "seller_id": NVARCHAR(32),
        "seller_zip_code_prefix": NVARCHAR(10),
        "seller_city": Unicode(100),
        "seller_state": NVARCHAR(2),
    }),
    "product_category_name_translation.csv": (
        "ecom_category_translation", {
            "product_category_name": Unicode(100),
            "product_category_name_english": Unicode(100),
        }
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import the Olist CSV dataset.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate CSV files and destination without writing to SQL Server.",
    )
    args = parser.parse_args()

    missing = [
        filename for filename in FILES
        if not (DATA_DIR / filename).is_file()
    ]
    if missing:
        print("Missing CSV files:")
        for filename in missing:
            print(f"  - {filename}")
        return 1

    settings = get_settings()

    # Connect to the dedicated database, not the configured simulator DB.
    engine = make_engine(
        settings, database=TARGET_DB, admin=False
    )

    try:
        inspector = inspect(engine)
        existing = set(inspector.get_table_names())

        target_tables = {table for table, _ in FILES.values()}
        collisions = sorted(existing & target_tables)
        if collisions:
            print("Import stopped: destination tables already exist:")
            for table in collisions:
                print(f"  - {table}")
            print("No tables were overwritten. Inspect the existing tables first.")
            return 2

        print(f"Target database: {TARGET_DB}")
        print(f"CSV directory: {DATA_DIR.resolve()}")
        print(f"Batch size: {CHUNK_SIZE:,}")
        print("Preflight passed: none of the target tables exist.")

        if args.dry_run:
            print("\\nDRY RUN: validating CSV data in batches; no database writes.")
            for filename, (table, schema) in FILES.items():
                total = 0
                for chunk in pd.read_csv(
                    DATA_DIR / filename,
                    chunksize=CHUNK_SIZE,
                    dtype={
                        column: "string"
                        for column in schema
                        if isinstance(schema[column], NVARCHAR)
                        or isinstance(schema[column], Unicode)
                    },
                ):
                    for column, sql_type in schema.items():
                        if isinstance(sql_type, DateTime):
                            raw = chunk[column]
                            parsed = pd.to_datetime(raw, errors="coerce")
                            invalid = raw.notna() & parsed.isna()
                            if invalid.any():
                                raise ValueError(
                                    f"{filename}: {invalid.sum()} invalid "
                                    f"timestamp(s) in {column}"
                                )
                            chunk[column] = parsed

                    total += len(chunk)

                print(f"  VALID: {filename} -> {table} ({total:,} rows)")

            print("\\nDRY RUN PASSED: no tables or rows were written.")
            return 0

        for filename, (table, schema) in FILES.items():
            path = DATA_DIR / filename
            print(f"\nImporting {filename} -> {table}")

            total = 0
            first_batch = True

            try:
                reader = pd.read_csv(
                    path,
                    chunksize=CHUNK_SIZE,
                    dtype={
                        column: "string"
                        for column in schema
                        if isinstance(schema[column], NVARCHAR)
                        or isinstance(schema[column], Unicode)
                    },
                )

                for chunk in reader:
                    # Convert known timestamp columns; invalid non-empty
                    # values fail rather than silently becoming missing.
                    for column, sql_type in schema.items():
                        if isinstance(sql_type, DateTime):
                            raw = chunk[column]
                            parsed = pd.to_datetime(raw, errors="coerce")
                            invalid = raw.notna() & parsed.isna()
                            if invalid.any():
                                raise ValueError(
                                    f"{filename}: {invalid.sum()} invalid "
                                    f"timestamp(s) in {column}"
                                )
                            chunk[column] = parsed

                    chunk.to_sql(
                        name=table,
                        con=engine,
                        if_exists="fail" if first_batch else "append",
                        index=False,
                        chunksize=1000,
                        dtype=schema,
                        method=None,
                    )
                    total += len(chunk)
                    first_batch = False

                print(f"  Imported rows: {total:,}")

            except Exception:
                print(
                    f"ERROR while importing {filename}. "
                    "Some tables may already have been imported. "
                    "Do not rerun blindly; inspect the destination first."
                )
                raise

        print("\nImport finished. Verifying destination row counts...")
        with engine.connect() as conn:
            for table, _ in FILES.values():
                count = conn.execute(
                    text(f"SELECT COUNT_BIG(*) FROM dbo.[{table}]")
                ).scalar_one()
                print(f"  {table}: {count:,}")

        print("\nImport completed. Compare these counts with source CSV row counts.")
        return 0

    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
