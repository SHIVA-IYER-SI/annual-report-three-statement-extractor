# Deployment Checklist

- [ ] Extract the standalone ZIP.
- [ ] Open the extracted folder and confirm `Dockerfile`, `render.yaml`, `requirements.txt`, and `app/` are visible at the same level.
- [ ] Create an empty GitHub repository.
- [ ] Upload the **contents** of the extracted folder to the repository root.
- [ ] Confirm GitHub shows `app/main.py` and `render.yaml`.
- [ ] Connect the GitHub repository in Render.
- [ ] Create the service from the Blueprint.
- [ ] Wait for the Docker build and deployment to finish.
- [ ] Open `/api/health` and confirm `status: ok`.
- [ ] Open the main website.
- [ ] Test with one native-text annual report.
- [ ] Confirm all three preview tabs appear.
- [ ] Download the Excel and confirm the three exact sheets.
- [ ] Use the deletion button after testing.

## Multi-report acceptance

- [ ] Select 1 file and confirm upload starts.
- [ ] Select 5 files for the same company and confirm one combined workbook is generated.
- [ ] Confirm selecting 11 files is rejected before upload.
- [ ] Confirm any file larger than 250 MB is rejected.
- [ ] Confirm files are deleted after expiry or by the Delete button.
