"""
ETL Pipeline for Taxi Trip Data

Connections:
"""

from airflow import DAG
from airflow.operators import BashOperator
from airflow.operators import PythonOperator
from airflow.operators import DatumLoadOperator
from airflow.operators import CreateStagingFolder, DestroyStagingFolder
from airflow.operators import FolderDownloadOperator, FileAvailabilitySensor
from airflow.operators import SlackNotificationOperator
from datetime import datetime, timedelta

# ============================================================
# Defaults - these arguments apply to all operators

default_args = {
    'owner': 'airflow',
    'on_failure_callback': SlackNotificationOperator.failed(),
}

pipeline = DAG('etl_taxi_trips_v4',
    start_date=datetime.now() - timedelta(days=1),
    schedule_interval='@weekly',
    default_args=default_args
)

# Extract
# -------
# 1. Create a staging folder
# 2. Check twice per day to see whether the data has been uploaded
# 3. When the data is available, download into the staging folder

make_staging = CreateStagingFolder(task_id='staging', dag=pipeline)

wait_for_verifone = FileAvailabilitySensor(task_id='wait_for_verifone', dag=pipeline,
    source_type='sftp',
    source_conn_id='phl-ftp-etl',
    source_path='/Taxi/verifone/*',

    poke_interval=60*60*12,  # 12 hours
    timeout=60*60*24*7,  # 1 week
)

wait_for_cmt = FileAvailabilitySensor(task_id='wait_for_cmt', dag=pipeline,
    source_type='sftp',
    source_conn_id='phl-ftp-etl',
    source_path='/Taxi/cmt/*',

    poke_interval=60*60*12,  # 12 hours
    timeout=60*60*24*7,  # 1 week
)

download_verifone = FolderDownloadOperator(task_id='download_verifone', dag=pipeline,
    source_type='sftp',
    source_conn_id='phl-ftp-etl',
    source_path='/Taxi/verifone',

    dest_path='{{ ti.xcom_pull("staging") }}/input/verifone',
)

download_cmt = FolderDownloadOperator(task_id='download_cmt', dag=pipeline,
    source_type='sftp',
    source_conn_id='phl-ftp-etl',
    source_path='/Taxi/cmt',

    dest_path='{{ ti.xcom_pull("staging") }}/input/cmt',
)

unzip_cmt = BashOperator(task_id='unzip_cmt', dag=pipeline,
    bash_command=
        'for f in $(ls {{ ti.xcom_pull("staging") }}/input/cmt/*.zip); '
        '  do unzip $f -d {{ ti.xcom_pull("staging") }}/input/cmt/; done'
)

unzip_verifone = BashOperator(task_id='unzip_verifone', dag=pipeline,
    bash_command=
        'for f in $(ls {{ ti.xcom_pull("staging") }}/input/verifone/*.zip); '
        '  do unzip $f -d {{ ti.xcom_pull("staging") }}/input/verifone/; done'       
)

download_hexbins = BashOperator(task_id='download_hexbins', dag=pipeline,
    bash_command=
        'wget https://github.com/CityOfPhiladelphia/trip-data-pipeline/raw/master/geo/hexagons_20160919.geojson'
        ' -O {{ ti.xcom_pull("staging") }}/input/hexbins.geojson'
)

# Transform & Load
# ----------------
# 0. Merge all of the downloaded files into one CSV
# 1. Insert the merged data into an Oracle table.
# 2. Update the anonymization mapping tables.
# 3. Generalize ("fuzzy") the pickup and dropoff locations and times.
# 4. Insert the anonymized and fuzzied data into a public table.

merge_and_norm = BashOperator(task_id='merge_and_norm', dag=pipeline,
    bash_command=
        'taxitrips.py normalize'
        '  --verifone "{{ ti.xcom_pull("staging") }}/input/verifone/*.csv"'
        '  --cmt "{{ ti.xcom_pull("staging") }}/input/cmt/*.csv" > '
        '{{ ti.xcom_pull("staging") }}/merged_trips.csv',
)

load_raw = DatumLoadOperator(task_id='load_raw', dag=pipeline,
    csv_path='{{ ti.xcom_pull("staging") }}/merged_trips.csv',
    db_conn_id='phl-warehouse-staging',
    db_table_name='taxi_trips',
)

fuzzy = BashOperator(task_id='fuzzy_time_and_loc', dag=pipeline,
    bash_command=
        'taxitrips.py fuzzy'
        ' --regions {{ ti.xcom_pull("staging") }}/input/hexbins.geojson'
        '  {{ ti.xcom_pull("staging") }}/merged_trips.csv > '
        '{{ ti.xcom_pull("staging") }}/fuzzied_trips.csv',
)

anonymize = BashOperator(task_id='anonymize', dag=pipeline,
    bash_command=
        'taxitrips.py anonymize '
        '  "{{ ti.xcom_pull("staging") }}/fuzzied_trips.csv" > '
        '{{ ti.xcom_pull("staging") }}/anonymized_trips.csv',
)

load_public = DatumLoadOperator(task_id='load_public', dag=pipeline,
    csv_path='{{ ti.xcom_pull("staging") }}/anonymized_trips.csv',
    db_conn_id='phl-warehouse-staging',
    db_table_name='taxi_trips',
)

cleanup_staging = DestroyStagingFolder(task_id='cleanup_staging', dag=pipeline,
    dir='{{ ti.xcom_pull("staging") }}',
)


# ============================================================
# Configure the pipeline's dag

make_staging  >>    wait_for_cmt     >>    download_cmt     >>    unzip_cmt     >>  merge_and_norm
make_staging  >>  wait_for_verifone  >>  download_verifone  >>  unzip_verifone  >>  merge_and_norm

merge_and_norm  >>  load_raw  >>  anonymize
merge_and_norm  >>  download_hexbins  >>    fuzzy    >>  anonymize

anonymize  >>  load_public  >>  cleanup_staging
