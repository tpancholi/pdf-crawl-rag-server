import logging
import os
import re
from typing import Annotated, List
from uuid import uuid4

import boto3
import botocore
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

import models
from database import SessionLocal, engine

# load environment variable
load_dotenv()

# logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# AWS S3 Configuration
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")

# validate AWS Credentials are set
if not all([AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION]):
	logger.critical("AWS credentials not set")
	raise RuntimeError("AWS credentials not set")

# Allowed CORS origins
origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
app = FastAPI(
	title="RAG Backend",
	description="API to support RAG functionality",
	version="1.0.0",
)


app.add_middleware(
	CORSMiddleware,
	allow_origins=origins,
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)


def get_db() -> SessionLocal:
	db = SessionLocal()
	try:
		yield db
	finally:
		db.close()


db_dependency = Annotated[Session, Depends(get_db)]
models.Base.metadata.create_all(bind=engine)

s3_client = boto3.client(
	"s3",
	aws_access_key_id=AWS_ACCESS_KEY_ID,
	aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
	region_name=AWS_REGION,
)


class FileUploadBase(BaseModel):
	file_name: str
	s3_file_name: str


class FileUploadModel(FileUploadBase):
	id: int
	model_config = ConfigDict(from_attributes=True)


def sanitize_filename(filename: str) -> str:
	"""Sanitize input filename to remove unsafce characters"""
	return re.sub(r"[^a-zA-Z0-9_.-]", "_", filename)


def generate_unique_file_name(filename: str) -> str:
	""" "Generate a unique filename using UUID"""
	ext = os.path.splitext(filename)[1]
	return f"{uuid4().hex}{ext}"


async def validate_pdf(file: UploadFile):
	"""Ensure the file is a valid PDF by magic bytes"""
	file.file.seek(0)
	header = await file.read(4)
	file.file.seek(0)
	if header != b"%PDF":
		raise HTTPException(status_code=400, detail="Invalid PDF file")


async def upload_file_to_s3(file: UploadFile, bucket: str, s3_key: str) -> str:
	"""Upload a file to an S3 bucket"""
	try:
		content = await file.read()
		s3_client.put_object(
			Bucket=bucket,
			key=s3_key,
			Body=content,
			ContentType=file.content_type,
			ACL="private",
		)
		return s3_key
	except botocore.exceptions.ClientError as error:
		logger.error(f"AWS S3 Error: {error}")
		raise HTTPException(status_code=500, detail=error.response["Error"]["Message"])
	except botocore.exceptions.ParamValidationError as error:
		raise ValueError("The parameter provided are incorrect: {}", format(error))
	except Exception as e:
		logger.error(f"Error uploading to s3: {str(e)}")
		raise HTTPException(status_code=500, detail="File upload failed")


def save_file_upload_details(db: Session, file_name: str, s3_file_name: str):
	"""Saves the file upload details to the database"""
	try:
		db_file_upload = models.FileUpload(
			file_name=file_name,
			s3_file_name=s3_file_name,
		)
		db.add(db_file_upload)
		db.commit()
		db.refresh(db_file_upload)
		return db_file_upload
	except Exception as e:
		logger.exception("Database error while saving file data")
		raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.post("/upload-pdf", response_model=FileUploadModel)
async def upload_pdf(db: db_dependency, file: UploadFile = File(...)):
	"""Route to upload a PDF file to an S3 bucket"""
	# Validate file type
	if file.content_type != "application/pdf":
		raise HTTPException(status_code=400, detail="Only PDF files are supported")

	await validate_pdf(file)

	# Validate file size (e.g., 5 MB limit)
	file.file.seek(0, 2)
	file_size = file.file.tell()
	file.file.seek(0)
	max_size = 5 * 1024 * 1024

	if file_size > max_size:
		raise HTTPException(status_code=400, detail="File size exceeds 5MB limit")

	# Generate Unique filename
	sanitized_name = sanitize_filename(file.file.name)
	unique_file_name = generate_unique_file_name(sanitized_name)
	s3_key = f"uploads/{unique_file_name}"

	try:
		# upload to s3
		new_s3_key = await upload_file_to_s3(file, S3_BUCKET_NAME, s3_key)
		if new_s3_key:
			# save file to db
			file_upload_record = save_file_upload_details(
				db, file_name=file.filename, s3_file_name=s3_key
			)
			return file_upload_record
		return None
	except Exception as e:
		logger.error(f"Upload failed: {str(e)}")
		raise HTTPException(status_code=500, detail="Upload failed")


@app.get("/files", response_model=List[FileUploadModel])
async def get_files(db: db_dependency, skip: int = 0, limit: int = 100):
	"""Get files uploaded to table"""
	try:
		files = (
			db.query(models.FileUpload)
			.order_by(models.FileUpload.upload_date.desc())
			.offset(skip)
			.limit(limit)
			.all()
		)
		return files
	except Exception as e:
		logger.exception("Error retrieving files")
		raise HTTPException(
			status_code=500, detail="Unable to fetch files from db: {}".format(str(e))
		)


@app.get("/health")
async def health_check():
	return {"status": "healthy"}
