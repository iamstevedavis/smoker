# Docker operations

**Upgrade:** `git pull && docker compose up -d --build` (or `docker compose pull && docker compose up -d` for the published image). The database lives on the `pitboss-data` volume and survives upgrades.

**Upgrading from the old `./data` bind mount:** earlier versions kept the
database in `./data` and ran as root. The named volume starts empty, so copy the
old database in once (and hand it to the container's non-root user, uid 10001)
before starting the new version:

```bash
docker compose down
docker compose up --no-start          # creates the pitboss-data volume
VOL=$(docker compose config | sed -n 's/^ *name: \(.*_pitboss-data\)$/\1/p')
docker run --rm -v "$VOL":/data -v "$PWD/data":/old:ro alpine \
  sh -c 'cp -a /old/. /data/ && chown -R 10001:10001 /data'
docker compose up -d
```

After checking the history is there, `./data` can be deleted.

**Back up** the database volume:

```bash
VOL=$(docker compose config | sed -n 's/^ *name: \(.*_pitboss-data\)$/\1/p')
docker run --rm -v "$VOL":/data -v "$PWD":/backup alpine \
  tar czf /backup/pitboss-data.tgz -C /data .
```

The first line asks compose for the volume's real name, `<project>_pitboss-data`.
The project defaults to the clone's folder name, so a clone from before the
rename uses `smoker_pitboss-data` and a new one `smokestack_pitboss-data`. Run
the commands from the clone's folder.
