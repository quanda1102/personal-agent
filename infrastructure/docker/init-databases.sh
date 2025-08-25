#!/bin/bash
set -e

# Create multiple databases for PostgreSQL
function create_user_and_database() {
    local database=$1
    echo "Creating database '$database'"
    
    if [ "$database" = "kong_db" ]; then
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
            CREATE DATABASE kong_db;
            GRANT ALL PRIVILEGES ON DATABASE kong_db TO $POSTGRES_USER;
EOSQL
    fi
}

if [ -n "$POSTGRES_MULTIPLE_DATABASES" ]; then
    echo "Multiple database creation requested: $POSTGRES_MULTIPLE_DATABASES"
    for db in $(echo $POSTGRES_MULTIPLE_DATABASES | tr ',' ' '); do
        if [ "$db" != "$POSTGRES_DB" ]; then
            create_user_and_database $db
        fi
    done
    echo "Multiple databases created"
fi
