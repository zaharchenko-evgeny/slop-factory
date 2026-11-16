# Progress UI assets

The build copies `public/` files into `factory/factory_ui/static/`.

After changing UI dependencies, refresh the shipped license notices from the lock file:

```sh
npm run notices
npm run build
```

The script skips development-only packages. It writes the production dependency notices and copies Geist's license into `public/licenses/`.
