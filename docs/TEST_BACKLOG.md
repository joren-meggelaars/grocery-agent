# Test backlog: things only a real shop visit can prove

Everything below is untried on a real phone. The automated tests (350+) cover the logic with fakes; they cannot show
whether the camera, the lighting, the real label wording or the offline behaviour work. Tick items off as you go and
note what happened; report the failures with the **capture number** (the number in the address, `/capture/<number>`)
or **receipt number** (`/receipts/<number>`).

## A. Before you leave (on wifi)

- [ ] Server is up to date: `git pull && docker compose up -d --build` on the VM, then `docker compose ps` shows db, app, worker.
- [ ] iPhone: open the site in Safari, **Share, Add to Home Screen**, and open the app from that icon.
- [ ] Open the scan page once while online and allow the camera. (This also saves the page for offline use.)
- [ ] The scan page shows the store list and the photo button first, the barcode second (optional).

## B. In the shop

**Scanner**
- [ ] B1. Scan an ordinary barcode (packaged product). Does it read within a couple of seconds? Note the lighting and the shop.
- [ ] B2. Scan a barcode that is small or curved (a tube, a bottle). Does it fail? If so, does typing the number work?
- [ ] B3. Deny-and-allow: if the camera asks for permission again, does the message make sense?

**Shelf labels**
- [ ] B4. **Photo only, no barcode** (the new default path): photograph a label and press Save. Does the product name come out right?
- [ ] B5. A plain label (one price). Are price, product name and price per kg/l right on the confirm screen?
- [ ] B6. A promotion label: "2 voor ...", "1+1 gratis", "25% korting", "2e halve prijs". Check *Promotion*, the text, and *Price per item with the promotion*.
- [ ] B7. A loyalty-card / app price (Bonus, Plus, Jumbo Extra, Lidl Plus). Is *Needs a loyalty card or app* ticked?
- [ ] B8. A label with a validity period ("t/m 27 september"). Is the *Valid until* date right (and the year not invented)?
- [ ] B9. A price per 100 g or per 100 ml. Is it shown per kg / per l on the confirm screen?
- [ ] B10. A blurry or partly hidden label: does the reader say so in the note, or fail with a clear message?

**Recognising products**
- [ ] B11. Scan a product you have bought before on a receipt. After you confirm, does the card say what you usually pay?
- [ ] B12. Same product, photo only, no barcode: is it recognised by name (name prefilled from your receipts)?
- [ ] B13. A barcode Open Food Facts does not know: the product name should come from the label instead.
- [ ] B14. The same product at a second shop (for example Lidl): does *Cheaper elsewhere* appear when it is cheaper?

**Offline**
- [ ] B15. Switch on airplane mode, capture two products. Does the counter at the top say 2 waiting? Is nothing lost?
- [ ] B16. Close the app, reopen it offline. Does the scan page still open and still show 2 waiting?
- [ ] B17. Switch airplane mode off and open the app. Do they upload by themselves (counter to 0, no duplicates under *To review*)?
- [ ] B18. Untick *Review right after saving*, scan several items, then work through *To review* afterwards.

**Turkish supermarket / bakery**
- [ ] B19. Photograph a chicken price label (may be handwritten). Is the per-kg price readable? If not, does *Enter it by hand* work?
- [ ] B20. After the visit: *Add Turkish supermarket purchase* with weight and price per kg. Is EUR 8.49 still prefilled or is it the last price you used?

## C. Receipts (ongoing)

- [ ] C1. Each receipt: are all lines found, are discounts separate negative lines, do weights and price per kg match?
- [ ] C2. The difference bar at the bottom of the review screen: zero when the receipt is right?
- [ ] C3. A receipt without a date on the photo: flagged and easy to fill in (this happened once, fine).
- [ ] C4. Third receipt from a shop you have used: known products show *Recognised from an earlier receipt*.
- [ ] C5. A long receipt in several photos (top to bottom).

## D. What to send back

For each failure: the number, what you expected, what you saw. If the reading was wrong, the raw answer is stored:

```bash
docker compose exec db psql -U grocery -d grocery \
  -c "select id, kind, subject_id, input_tokens, output_tokens, round(cost_est_eur::numeric,4) as eur, parsed_ok, error from extractions order by id desc limit 15;"
```

## F. Overview and regulars (after a few saved receipts)

- [ ] F1. **Monthly overview:** does the food spend equal what you expect? (Sum of the saved receipts of this month minus household items.)
- [ ] F2. Is a discounted product reduced in its own category (for example a chicken with a bonus line)?
- [ ] F3. Are the category and store bars plausible, and does *Table* show the same numbers as the bars?
- [ ] F4. On the iPhone: do the tooltips appear when you tap a bar, and is nothing cut off at the edges of the screen?
- [ ] F5. **What I buy most:** are the same products counted once per receipt, and is the usual price right (per kg for chicken)?
- [ ] F6. Products without a name are mentioned at the bottom: name them on the receipt and check they appear.
- [ ] F7. Dark mode: charts and the meter stay readable.
- [ ] F8. Is a receipt from a past month counted in that month (change the date on the review screen and check both months)?

## H. Products at home (one evening, at home)

- [ ] H1. **Scan products at home, Start camera:** hold ten products' barcodes in front of the camera one after the other. Does each one appear in the result list, once, without repeats while you hold it still?
- [ ] H2. Do known barcodes (scanned in a shop before) go on the list at once, and unknown ones under *waiting for a name*?
- [ ] H3. **Name scanned products:** is the Open Food Facts name filled in and reasonable? Are the categories easy to pick?
- [ ] H4. Type a product name that you also have on receipts: does the suggestion list show it, so they end up as one product?
- [ ] H5. Mark a few products *used a lot* (the star). Are they at the top of **Products at home**?
- [ ] H6. A product without a barcode (bread, fruit): does *Add a product without a barcode* work?
- [ ] H7. After a few receipts: does each product show what you last paid and where? Do you see *Seen cheaper* when a Lidl label was cheaper?
- [ ] H8. In the shop: scan the label of a product on the list. Does the confirm screen say *You have this at home*?

## I. Look and feel (on the iPhone)

- [ ] I1. Open the app from the home-screen icon: is the status bar and the top of the screen black, without a white strip?
- [ ] I2. The bottom tab bar: Home, Receipts, Scan (raised blue button), Overview, More. Is it clear of the home indicator, and does *More* open the full menu?
- [ ] I3. Does the review screen's *Lines / Total / Difference* bar stay above the tab bar while you scroll a long receipt?
- [ ] I4. Dark mode (iPhone setting): pages stay readable, the hero card and charts look right.
- [ ] I5. On the scan page, does nothing jump when the *waiting to upload* bar appears or disappears?
- [ ] I6. After the update the old look may show once from the phone's cache: open the scan page online once and it refreshes.

## G. Known gaps on my side (not tested, not blocking)

- The real Claude call for shelf labels (prompt `shelf_v1`) has only run against a stub. Expect one tuning round.
- The scanner runs on the vendored ZXing library on iPhone; its speed in a busy aisle is unknown.
- iOS cannot upload in the background: the queue empties when the app is opened again with a connection.
- Long term: try Haiku 4.5 for labels if the readings are good (`LLM_MODEL_SHELF`), to save cost.
