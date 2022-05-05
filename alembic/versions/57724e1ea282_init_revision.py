"""init revision

Revision ID: 57724e1ea282
Revises: faec5c0c55d3
Create Date: 2022-02-24 20:09:16.862098

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


# revision identifiers, used by Alembic.
revision = "57724e1ea282"
down_revision = None
branch_labels = None
depends_on = None


conn = op.get_bind()
inspector = Inspector.from_engine(conn)
tables = inspector.get_table_names()


def create_table(table_name, *params):
    if table_name in tables:
        return
    op.create_table(table_name, *params)


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    create_table(
        "dicom_files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("time", sa.DateTime(), nullable=True),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("file_uid", sa.String(), nullable=True),
        sa.Column("series_uid", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    create_table(
        "dicom_series",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("time", sa.DateTime(), nullable=True),
        sa.Column("series_uid", sa.String(), nullable=True),
        sa.Column("tag_patientname", sa.String(), nullable=True),
        sa.Column("tag_patientid", sa.String(), nullable=True),
        sa.Column("tag_accessionnumber", sa.String(), nullable=True),
        sa.Column("tag_seriesnumber", sa.String(), nullable=True),
        sa.Column("tag_studyid", sa.String(), nullable=True),
        sa.Column("tag_patientbirthdate", sa.String(), nullable=True),
        sa.Column("tag_patientsex", sa.String(), nullable=True),
        sa.Column("tag_acquisitiondate", sa.String(), nullable=True),
        sa.Column("tag_acquisitiontime", sa.String(), nullable=True),
        sa.Column("tag_modality", sa.String(), nullable=True),
        sa.Column("tag_bodypartexamined", sa.String(), nullable=True),
        sa.Column("tag_studydescription", sa.String(), nullable=True),
        sa.Column("tag_seriesdescription", sa.String(), nullable=True),
        sa.Column("tag_protocolname", sa.String(), nullable=True),
        sa.Column("tag_codevalue", sa.String(), nullable=True),
        sa.Column("tag_codemeaning", sa.String(), nullable=True),
        sa.Column("tag_sequencename", sa.String(), nullable=True),
        sa.Column("tag_scanningsequence", sa.String(), nullable=True),
        sa.Column("tag_sequencevariant", sa.String(), nullable=True),
        sa.Column("tag_slicethickness", sa.String(), nullable=True),
        sa.Column("tag_contrastbolusagent", sa.String(), nullable=True),
        sa.Column("tag_referringphysicianname", sa.String(), nullable=True),
        sa.Column("tag_manufacturer", sa.String(), nullable=True),
        sa.Column("tag_manufacturermodelname", sa.String(), nullable=True),
        sa.Column("tag_magneticfieldstrength", sa.String(), nullable=True),
        sa.Column("tag_deviceserialnumber", sa.String(), nullable=True),
        sa.Column("tag_softwareversions", sa.String(), nullable=True),
        sa.Column("tag_stationname", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_uid"),
    )

    create_table(
        "dicom_series_map",
        sa.Column("id_file", sa.Integer(), nullable=False),
        sa.Column("id_series", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id_file"),
    )
    create_table(
        "file_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("time", sa.DateTime(), nullable=True),
        sa.Column("dicom_file", sa.Integer(), nullable=True),
        sa.Column("event", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    create_table(
        "mercure_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("time", sa.DateTime(), nullable=True),
        sa.Column("sender", sa.String(), nullable=True),
        sa.Column("event", sa.String(), nullable=True),
        sa.Column("severity", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    create_table(
        "series_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("time", sa.DateTime(), nullable=True),
        sa.Column("sender", sa.String(), nullable=True),
        sa.Column("event", sa.String(), nullable=True),
        sa.Column("series_uid", sa.String(), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=True),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("info", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    create_table(
        "series_sequence_data",
        sa.Column("uid", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("uid"),
    )
    create_table(
        "webgui_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("time", sa.DateTime(), nullable=True),
        sa.Column("sender", sa.String(), nullable=True),
        sa.Column("event", sa.String(), nullable=True),
        sa.Column("user", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("webgui_events")
    op.drop_table("series_sequence_data")
    op.drop_table("series_events")
    op.drop_table("mercure_events")
    op.drop_table("file_events")
    op.drop_table("dicom_series_map")
    op.drop_table("dicom_series")
    op.drop_table("dicom_files")
    # ### end Alembic commands ###
