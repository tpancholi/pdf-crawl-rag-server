import os
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
import botocore
import boto3

from dotenv import load_dotenv
from typing import Annotated, List
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from pydantic import BaseModel

import models
from database import SessionLocal, engine

load_dotenv()

# AWS S3 Configuration
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
	title="RAG Backend",
	description="API to support RAG functionality",
	version="1.0.0",
)
origins = [
	"http://localhost:3000",
]

app.add_middleware(
	CORSMiddleware,
	allow_origins=origins,
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)


class FileUploadBase(BaseModel):
	file_name: str
	s3_file_name: str


class FileUploadModel(FileUploadBase):
	id: int

	class Config:
		orm_mode = True


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


def generate_unique_file_name(filename: str) -> str:
	""" "Generate a unique filename to prevent overwrites"""
	timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
	name, ext = os.path.splitext(filename)
	return f"{name}_{timestamp}{ext}"


async def upload_file_to_s3(file: UploadFile, bucket: str, s3_key: str) -> str:
	"""Upload a file to an S3 bucket"""
	try:
		s3_client.upload_fileobj(
			file.file,
			bucket,
			s3_key,
			ExtraArgs={
				"ContentType": file.content_type,
				"ACL": "private",
			},
		)
		return s3_key
	except botocore.exceptions.ClientError as error:
		logger.error(
			f"Error code {error.response['Error']['Code']} and Error Message {error.response['Error']['Message']}"
		)
		raise HTTPException(status_code=500, detail=error.response["Error"]["Message"])
	except botocore.exceptions.ParamValidationError as error:
		raise ValueError("The parameter provided are incorrect: {}", format(error))
	except Exception as e:
		logger.error(f"Error uploading to s3: {str(e)}")
		raise HTTPException(status_code=500, detail="File upload failed")


def save_file_upload_details(db: Session, file_name: str, s3_file_name: str):
	"""Saves the file upload details to the database"""
	db_file_upload = models.FileUpload(
		file_name=file_name,
		s3_file_name=s3_file_name,
	)
	db.add(db_file_upload)
	db.commit()
	db.refresh(db_file_upload)
	return db_file_upload


@app.post("/upload-pdf", response_model=FileUploadModel)
async def upload_pdf(db: db_dependency, file: UploadFile = File(...)):
	"""Route to upload a PDF file to an S3 bucket"""
	# Validate file type
	if file.content_type != "application/pdf":
		raise HTTPException(status_code=400, detail="Only PDF files are supported")

	# Validate file size (e.g., 5 MB limit)
	max_size = 5 * 1024 * 1024
	file.file.seek(0, 2)
	file_size = file.file.tell()
	file.file.seek(0)

	if file_size > max_size:
		raise HTTPException(status_code=400, detail="File size exceeds 5MB limit")

	# Generate Unique filename
	unique_file_name = generate_unique_file_name(file.filename)
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
	files = (
		db.query(FileUploadModel)
		.order_by(models.FileUpload.upload_date.desc())
		.offset(skip)
		.limit(limit)
		.all()
	)
	return files


@app.get("/health")
async def health_check():
	return {"status": "healthy"}
