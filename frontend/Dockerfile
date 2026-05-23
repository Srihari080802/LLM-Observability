# Frontend image: just nginx serving one static HTML file.
# No build step because the UI is a single self-contained file.
FROM nginx:1.27-alpine
COPY frontend/index.html /usr/share/nginx/html/index.html
EXPOSE 80
