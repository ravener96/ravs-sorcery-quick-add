this program lets you add cards very quickly to the sorcerytcg.com/collection using firefox.
when starting the program first time you may be prompted to log in, but afterwards it should log in directly, as it maintains a cookie.



```
usage: sorcery_collector.py [-h] --set SET [--foil] [-d] [--cleanup] file

Add cards from a text file to your sorcerytcg.com collection.

positional arguments:
  file           text file with one (partial) card name per line

options:
  -h, --help     show this help message and exit
  --set SET      set name or code, e.g. beta, alpha, 'arthurian legends', gothic
  --foil         add the foil printings (default: non-foil)
  -d, --dry-run  only show what would be added; no browser
  --cleanup      delete lines from the card file once they have been added successfully
```

the file could be named something like cards.txt, and the format is that of one entry per line:

```
templar
druid
bruin
```

partial matches are accepted, but if multiple cards hit you will be asked to select which card is the right one:

```
templ
dru
brui
```

you can add multiple of each card by adding a number in front

```
2 templar
3 druid
4 bruin
```

if you select --cleanup the script deletes lines that were successfully added, which makes it easier to remedy mistyped cards.
typos can also be fixed in the program window by clicking the errant text field and writing something valid.
