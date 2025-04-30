import datetime

from database import Base
from sqlalchemy import Column, Integer, String, DateTime


class FileUpload(Base):
	__tablename__ = "file_uploads"

	id = Column(Integer, primary_key=True, autoincrement=True, index=True)
	file_name = Column(String, index=True)
	s3_file_name = Column(String, index=True)
	upload_date = Column(DateTime, default=datetime.datetime.now(datetime.UTC))
