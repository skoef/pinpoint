## v0.1.0 (2026-09-10)

### Feat

- add sqlite database syncing to S3
- add support for postgres database
- play sound when waypoint is reached
- allow to customize completion text
- support running on EC2 instances
- add health check endpoints
- support images at waypoints
- require login to manage routes
- support deactivating and deleting routes
- add Dutch translations
- initial commit

### Fix

- improve s3 image storage description
- prefix waypoint images with route id, set default proximity to 10m and remove images from proximity waypoints
- use AWS_S3_REGION_NAME in stead of AWS_S3_ENDPOINT_URL
- fix S3 image links
- fix showing custom labels in tooltips
- show waypoint labels if available when hovering waypoints in route editor
- show questions only when arrived at the location
- allow to set CRSF trusted origins

### Refactor

- advance between waypoints without page reload
- rename application to Pinpoint
