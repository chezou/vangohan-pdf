# Vangohan Recipe to PDF

Install uv first, and then run:

```sh
./fetch_vangohan.py
```

If you execute locally, run:

```sh
$ python3 -m venv .venv
$ source .venv/bin/activate
$ pip install -r requirements.txt -c constraints.txt
$ python fetch_vangohan.py
$ ls results
vangohan.pdf  vangohan_en.pdf
```

Or, you can run by creating Docker image as:

```sh
$ docker build -t vangohan-pdf:latest ./
$ mkdir tmp
$ docker run --rm -it -t -v $(pwd)/tmp:/opt/app/results vangohan-pdf:latest
$ ls tmp
vangohan.pdf  vangohan_en.pdf
```
