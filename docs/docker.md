# Docker operations

**Upgrade:** `git pull && docker compose up -d --build` (or `docker compose pull && docker compose up -d` for the published image). The database lives on the `smokestack-data` volume and survives upgrades.

**Upgrading from the old `./data` bind mount:** earlier versions kept the
database in `./data` as `pitboss.sqlite` and ran as root. The named volume
starts empty, so copy the old database in once, under its new name
`smokestack.sqlite` (and hand it to the container's non-root user, uid 10001),
before starting the new version:

```bash
docker compose down
docker compose up --no-start          # creates the smokestack-data volume
VOL=$(docker compose config | sed -n 's/^ *name: \(.*_smokestack-data\)$/\1/p')
docker run --rm -v "$VOL":/data -v "$PWD/data":/old:ro alpine \
  sh -c 'cp -a /old/. /data/ && mv /data/pitboss.sqlite /data/smokestack.sqlite && chown -R 10001:10001 /data'
docker compose up -d
```

After checking the history is there, `./data` can be deleted.

**Back up** the database volume:

```bash
VOL=$(docker compose config | sed -n 's/^ *name: \(.*_smokestack-data\)$/\1/p')
docker run --rm -v "$VOL":/data -v "$PWD":/backup alpine \
  tar czf /backup/smokestack-data.tgz -C /data .
```

The first line asks compose for the volume's real name, `<project>_smokestack-data`.
The project defaults to the clone's folder name, so a clone from before the
rename uses `smoker_smokestack-data` and a new one `smokestack_smokestack-data`. Run
the commands from the clone's folder.
