---
license: cc-by-4.0
configs:
- config_name: train
  data_files:
  - split: train
    path: train.csv
- config_name: validation
  data_files:
  - split: validation
    path: validation.csv
- config_name: test
  data_files:
  - split: test
    path: test.csv
task_categories:
- text-generation
- text2text-generation
language:
- en
---

# TravelPlanner Dataset

TravelPlanner is a benchmark crafted for evaluating language agents in tool-use and complex planning within multiple constraints. (See our [paper](https://arxiv.org/pdf/2402.01622.pdf) for more details.)

## Introduction

In TravelPlanner, for a given query, language agents are expected to formulate a comprehensive plan that includes transportation, daily meals, attractions, and accommodation for each day.

TravelPlanner comprises 1,225 queries in total. The number of days and hard constraints are designed to test agents' abilities across both the breadth and depth of complex planning.

## Split

<b>Train Set</b>: 5 queries with corresponding human-annotated plans for group, resulting in a total of 45 query-plan pairs. This set provides the human annotated plans as demonstrations for in-context learning.

<b>Validation Set</b>: 20 queries from each group, amounting to 180 queries in total. There is no  human annotated plan in this set.

<b>Test Set</b>: 1,000 randomly distributed queries. To avoid data contamination, we only provide the level, days, and natural language query fields.

## Record Layout

- "org": The city from where the journey begins.
- "dest": The destination city.
- "days": The number of days planned for the trip.
- "visiting_city_number":  The total number of cities included in the itinerary.
- "date": The specific date when the travel is scheduled.
- "people_number": The total number of people involved in the travel.
- "local_constraint": The local hard constraint, including house rule, cuisine, room type and transportation.
- "query":  A natural language description or request related to the travel plan.
- "level": The difficulty level, which is determined by the number of hard constraints.
- "annotated_plan":  A detailed travel plan annotated by a human, ensuring compliance with all common sense requirements and specific hard constraints.
- "reference_information": Reference information for "sole-planning" mode.

## Citation

If our paper or related resources prove valuable to your research, we kindly ask for citation. Please feel free to contact us with any inquiries.

```bib
@article{Xie2024TravelPlanner,
  author    = {Jian Xie, Kai Zhang, Jiangjie Chen, Tinghui Zhu, Renze Lou, Yuandong Tian, Yanghua Xiao, Yu Su},
  title     = {TravelPlanner: A Benchmark for Real-World Planning with Language Agents},
  journal   = {arXiv preprint arXiv: 2402.01622},
  year      = {2024}
}
```