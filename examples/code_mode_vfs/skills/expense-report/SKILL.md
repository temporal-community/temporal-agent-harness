---
name: expense-report
description: Turn a list of expenses into a categorized expense report with totals.
---

# Expense report

1. Collect the expenses as `date,description,amount` rows. Use the user's rows if they gave
   any; otherwise use `/skills/expense-report/sample-expenses.csv`.
2. Assign every row a category using the rules in
   `/skills/expense-report/reference/categories.md`. A row that matches no rule is `Other`.
3. Write the report to `/workspace/expense-report.md`:
   - a `# Expense report` heading;
   - one `## <Category>` section per category, largest total first, listing its rows;
   - a final line `**Total: $<sum>**`, with two decimals.
4. Reply with the category totals and the path of the report.
